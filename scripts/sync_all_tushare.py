# -*- coding: utf-8 -*-
"""全量 Tushare 下载编排：遍历可下载数据集，断点续传，错误隔离，分层日志。

设计意图（Why 分层）：
- by=date/single 数据集单次小（按日/单期拉，快、省积分），先全量——覆盖龙虎榜/融资融券/
  北向/解禁/停牌/宏观/交易所统计/银行间等。
- by=symbol 数据集全市场 × ~5000 标的，极慢且积分消耗大（财务/筹码/股东/ETF），后跑，
  且支持 --limit-symbols 控范围（评估或下子集如沪深300）。
- 断点续传 resume=True：shard 已存在跳过，中断/积分耗尽后重跑只补未下的，不重复花积分。
- 错误隔离：单数据集异常不影响其他；积分/限频/权限致命错误停整批（避免无谓重试烧积分）。

用法：
  python scripts/sync_all_tushare.py --batch quick --years 10        # 先跑快批（by=date/宏观）
  python scripts/sync_all_tushare.py --batch slow --limit-symbols 300 # 慢批子集（沪深300）
  python scripts/sync_all_tushare.py --batch all --years 10           # 全量（快+慢，慢批全市场）
"""
from __future__ import annotations
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # 触发 .env 加载（python-dotenv）
from config import TUSHARE_DATASETS
from data.tushare_sync import sync_dataset, resolve_symbols

# 致命错误关键词：命中则停整批（避免积分耗尽后继续无谓重试烧积分）
_FATAL = ["积分", "频", "limit", "quota", "权限", "无权限", "没有接口"]


def classify() -> tuple[list[str], list[str]]:
    """按 by 模式分快慢批，排除 _unavailable。"""
    quick, slow = [], []
    for k, cfg in TUSHARE_DATASETS.items():
        if cfg.get("_unavailable"):
            continue
        (slow if cfg.get("by") == "symbol" else quick).append(k)
    return quick, slow


def run_batch(keys: list[str], start: str, end: str, limit_symbols, log) -> tuple[list, list]:
    """跑一批数据集，错误隔离 + 致命错误停批。返回 (ok列表, [(key,msg)] 失败列表)。"""
    ok, fail = [], []
    for i, key in enumerate(keys, 1):
        cfg = TUSHARE_DATASETS[key]
        # by=symbol：按 cfg['universe'] 自动路由标的池（stock/etf/index），不再统一喂股票列表。
        # Why：ETF(fund_*)/指数(index_*)若喂股票列表，接口返空→df.empty→continue，静默落空
        # （湖缺数据却无感知）。resolve_symbols 按 universe 字段选对的 loader，slow 批三类各走各的。
        syms = None
        if cfg.get("by") == "symbol":
            try:
                syms = resolve_symbols(key, limit_symbols)
                print(f"[universe] {key} universe={cfg.get('universe', 'stock')} → {len(syms)} 标的",
                      file=log, flush=True)
            except Exception as e:
                print(f"[universe] {key} 加载失败 {type(e).__name__}: {e}", file=log, flush=True)
        t0 = time.time()
        try:
            sync_dataset(key, start, end, symbols=syms, resume=True)
            dt = time.time() - t0
            print(f"[{i}/{len(keys)}] OK   {key:20s} {dt:6.0f}s", file=log, flush=True)
            ok.append(key)
        except Exception as e:
            dt = time.time() - t0
            msg = str(e)[:140]
            print(f"[{i}/{len(keys)}] FAIL {key:20s} {dt:6.0f}s {type(e).__name__}: {msg}",
                  file=log, flush=True)
            fail.append((key, msg))
            if any(w in msg for w in _FATAL):
                print("!!! 致命错误（积分/限频/权限），停止本批剩余，已下数据由断点续传保留",
                      file=log, flush=True)
                break
    return ok, fail


def main():
    ap = argparse.ArgumentParser(description="全量 Tushare 下载编排")
    ap.add_argument("--batch", choices=["quick", "slow", "all"], default="all")
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--limit-symbols", type=int, default=None,
                    help="by=symbol 仅前 N 标的（评估/子集，None=全市场）")
    ap.add_argument("--log", default="data_lake/.syncing/sync_all.log")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * args.years)).strftime("%Y-%m-%d")
    quick, slow = classify()
    print(f"批次={args.batch} years={args.years} limit_symbols={args.limit_symbols}", flush=True)
    print(f"quick({len(quick)}): {quick}", flush=True)
    print(f"slow({len(slow)}): {slow}", flush=True)

    with open(args.log, "w", encoding="utf-8") as log:
        print(f"\n=== START {datetime.now()} | 区间 {start}~{end} | batch={args.batch} ===\n",
              file=log, flush=True)
        all_ok, all_fail = [], []
        if args.batch in ("quick", "all"):
            print(">>> QUICK 批（by=date/single，快） <<<", file=log, flush=True)
            ok, fail = run_batch(quick, start, end, args.limit_symbols, log)
            all_ok += ok; all_fail += fail
        if args.batch in ("slow", "all"):
            print("\n>>> SLOW 批（by=symbol，全市场耗时） <<<", file=log, flush=True)
            ok, fail = run_batch(slow, start, end, args.limit_symbols, log)
            all_ok += ok; all_fail += fail
        print(f"\n=== DONE {datetime.now()} | OK {len(all_ok)} | FAIL {len(all_fail)} ===",
              file=log, flush=True)
        if all_fail:
            print("失败清单:", [k for k, _ in all_fail], file=log, flush=True)


if __name__ == "__main__":
    main()
