# scripts/calibrate_min_rr.py
# -*- coding: utf-8 -*-
"""min_rr_ratio 数据驱动定标：跑近 N 年全市场 replay → 产出建议值。

物理意图（Phase3+ 待办⑤）：
    Phase 2 Bug4 修后标准 W 底新公式 rr≈1.4 < 生产默认 min_rr_ratio=3.0，发不出计划。
    本脚本跑真实历史数据 replay，据胜率/平均盈亏比（_recommend_min_rr）给出数据驱动的
    生产 min_rr_ratio 建议值，人工据以改 caisen/config.py 默认。

用法：
    python scripts/calibrate_min_rr.py                # 默认近 3 年全市场（耗时几十分钟~几小时）
    python scripts/calibrate_min_rr.py --years 1      # 近 1 年
    python scripts/calibrate_min_rr.py --sample 300   # 近 3 年随机采样 300 标的（快速）

输出：n_hits/胜率/平均盈亏比/最大回撤/min_rr_ratio 建议 + rr 分布直方图文本。
"""
from __future__ import annotations
import os
import sys
import argparse
from datetime import datetime, timedelta

# 加项目根到 sys.path（脚本可从任意 cwd 直接运行）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from caisen.config import StrategyConfig
from caisen.risk import RiskManager
from caisen import backtest_replay
from data.lake_reader import DataLakeReader
from config import LAKE_CONFIG
from server.services.caisen_service import _load_price_data


def main(years: int, sample: int | None) -> None:
    # 1. 确保 daily 湖已 load 进 reader（脚本独立运行，不经过 server lifespan）
    reader = DataLakeReader.get_instance()
    daily_path = LAKE_CONFIG["lakes"]["daily"]
    if not reader.loaded or "daily" not in reader.lakes():
        print(f"加载 daily 湖：{daily_path} ...")
        reader.load(daily_path, key="daily")
    if not reader.loaded:
        print("ERROR：daily 湖未加载（parquet 缺失？），定标中止。")
        return

    # 2. 确定回放区间（近 N 年）+ universe
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    all_symbols = reader.symbols("daily")
    universe = all_symbols
    if sample and sample < len(all_symbols):
        universe = list(pd.Series(all_symbols).sample(n=sample, random_state=42))
    print(f"定标区间 {start} ~ {end}，标的数 {len(universe)}（全市场 {len(all_symbols)}）")

    # 3. 装配 price_data（_load_price_data 自动 amount 转元 + 全历史段）
    price_data = _load_price_data(universe, end)
    if not price_data:
        print("ERROR：price_data 装配为空，定标中止。")
        return
    print(f"装配 price_data：{len(price_data)} 标的")

    # 4. 跑 replay（用宽松 min_rr_ratio=0 收集尽可能多命中，统计真实 rr 分布）
    cfg = StrategyConfig(min_rr_ratio=0.0)
    risk = RiskManager(cfg)
    report = backtest_replay.replay(
        price_data, cfg, risk, start=start, end=end, aum=1_000_000.0,
    )

    # 5. 打印报告
    print("\n========== 定标报告 ==========")
    print(f"命中笔数 n_hits     : {report.n_hits}")
    print(f"胜率 win_rate       : {report.win_rate:.1%}")
    print(f"平均盈亏比 avg_rr   : {report.avg_rr:.3f}")
    print(f"最大回撤 max_dd     : {report.max_drawdown:.3f}")
    print(f"平均持仓天数        : {report.avg_holding_bars:.1f}")
    print(f"形态分布            : {report.pattern_dist}")
    print(f"\nmin_rr_ratio 建议   : {report.min_rr_ratio_recommendation}")

    # 6. rr 分布直方图（文本）
    hits = report.metadata.get("hits", [])
    if hits:
        rrs = pd.Series([h["rr"] for h in hits])
        print(f"\nrr 分布（n={len(rrs)}）：mean={rrs.mean():.3f} median={rrs.median():.3f} "
              f"std={rrs.std():.3f}")
        print(f"rr 分位：10%={rrs.quantile(0.1):.3f} 50%={rrs.quantile(0.5):.3f} "
              f"90%={rrs.quantile(0.9):.3f}")
        # 各阈值下的命中率（辅助选 min_rr_ratio）
        for thr in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
            pct = (rrs >= thr).mean()
            print(f"  rr >= {thr} : {pct:.1%} 命中保留 ({(rrs>=thr).sum()}/{len(rrs)})")
    print("==============================\n")
    print("据上述建议值，改 caisen/config.py 的 min_rr_ratio 默认（3.0 → 建议值）。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="min_rr_ratio 数据驱动定标（近 N 年全市场 replay）")
    ap.add_argument("--years", type=int, default=3, help="回放年数（默认 3）")
    ap.add_argument("--sample", type=int, default=None, help="随机采样标的数（缺省全市场）")
    args = ap.parse_args()
    main(years=args.years, sample=args.sample)
