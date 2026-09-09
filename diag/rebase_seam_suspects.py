# -*- coding: utf-8 -*-
"""daily 湖基线缝修复（2026-09-09 排查实锤后的存量治理）。

背景：diag/audit_adj_consistency.py 在 a_shares_daily 检出 145 处 qfq 不可达跳变
（|日环比|>35%、此前 ≥10 根 K 线；双创 51 只，+36~54% 持续性移位而非单根毛刺，
部分缝日精确命中除权日，簇日期跨标的共享）——判定为 2026-08-28 adj 硬闸落地前
的混合基线损伤（未复权/旧基线行成段混入 qfq 历史）。修复 = 对涉事标的按
data/tools/sync_daily_incremental._recompute_symbol 全历史重定基（夜班除权重算
同款机器，raw × adj / latest 单一基线重建）。

流程：检出嫌疑（默认双创=策略语料域；--all 含主板/北交所）→ 逐只重算 → 行数
守卫（新行 < 旧行 95% 视为拉取残缺，跳过该只不动湖）→ 备份涉事标的旧行 →
safe_overwrite 落湖 → 复检（期望命中归零）。

默认 dry-run（只列嫌疑不写湖）；--apply 才写。备份落
diag/lake_daily_backup_<ts>.parquet（可整段还原）。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # diag 直跑 → 仓库根

LAKE = "data_lake/a_shares_daily.parquet"
CHUANGKE = ("300", "301", "688", "689")


def detect_seam_symbols(df, jump_thr=0.35, min_bars=10, boards=None):
    """|日环比|>jump_thr 且此前 ≥min_bars 根 K 线的嫌疑标的 → {symbol: n_hits}。"""
    g = df.groupby(level="symbol")["close"]
    pct = g.pct_change()
    nb = g.cumcount()
    mask = (pct.abs() > jump_thr) & (nb >= min_bars) & pct.notna()
    syms = df.index.get_level_values("symbol")[mask]
    if boards is not None:
        syms = syms[[s.split(".")[0].startswith(boards) for s in syms]]
    vc = syms.value_counts()
    return dict(vc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写湖（默认 dry-run）")
    ap.add_argument("--all", action="store_true",
                    help="修复全市场嫌疑（默认仅双创=策略语料域）")
    ap.add_argument("--jump", type=float, default=0.35)
    args = ap.parse_args()

    df = pd.read_parquet(LAKE)
    boards = None if args.all else CHUANGKE
    suspects = detect_seam_symbols(df, jump_thr=args.jump, boards=boards)
    print(f"[rebase] 嫌疑标的 {len(suspects)} 只，命中 {sum(suspects.values())} 处"
          f"（域={'全市场' if args.all else '双创'}，阈值 {args.jump}）")
    for s, n in sorted(suspects.items(), key=lambda kv: -kv[1])[:20]:
        print(f"    {s}: {n}")
    if not suspects:
        print("[rebase] 无嫌疑，退出")
        return
    if not args.apply:
        print("[rebase] dry-run 结束（--apply 执行修复）")
        return

    from data._tushare_compat import get_pro
    from data.integrity import safe_overwrite
    from data.tools.sync_daily_incremental import _recompute_symbol

    pro = get_pro()
    todayc = datetime.today().strftime("%Y%m%d")
    backup = df[df.index.get_level_values("symbol").isin(suspects)].sort_index()
    ts = f"{datetime.now():%Y%m%d_%H%M%S}"
    backup_path = f"diag/lake_daily_backup_{ts}.parquet"
    backup.to_parquet(backup_path)
    print(f"[rebase] 备份 {len(backup)} 行 → {backup_path}")

    combined = df[~df.index.get_level_values("symbol").isin(suspects)]
    fixed_syms, skipped = [], []
    for i, (sym, nhit) in enumerate(sorted(suspects.items(), key=lambda kv: -kv[1]), 1):
        old_n = int((backup.index.get_level_values("symbol") == sym).sum())
        try:
            fixed = _recompute_symbol(pro, sym, todayc)
        except Exception as e:
            print(f"  [{i}/{len(suspects)}] {sym} 重算异常，跳过：{e}")
            skipped.append(sym)
            continue
        if fixed.empty:
            print(f"  [{i}/{len(suspects)}] {sym} 重算返空（停牌/退市/接口），跳过")
            skipped.append(sym)
            continue
        if len(fixed) < 0.95 * old_n:
            print(f"  [{i}/{len(suspects)}] {sym} 行数守卫触发"
                  f"（新 {len(fixed)} < 旧 {old_n}×95%），跳过不动湖")
            skipped.append(sym)
            continue
        combined = pd.concat([combined, fixed])
        fixed_syms.append(sym)
        print(f"  [{i}/{len(suspects)}] {sym} 重定基 {len(fixed)} 行（旧 {old_n}）")
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    if not fixed_syms:
        print("[rebase] 全部跳过，湖未改动")
        return
    safe_overwrite(LAKE, combined)
    print(f"[rebase] 落湖完成：修复 {len(fixed_syms)} 只，跳过 {len(skipped)} 只，"
          f"总行数 {len(df)} → {len(combined)}")

    # 复检（同域）：期望命中归零
    after = pd.read_parquet(LAKE)
    remain = detect_seam_symbols(after, jump_thr=args.jump, boards=boards)
    print(f"[rebase] 复检：剩余嫌疑 {len(remain)} 只 / {sum(remain.values())} 处"
          + (f"（top: {list(remain.items())[:5]}）" if remain else " ✓ 干净"))


if __name__ == "__main__":
    main()
