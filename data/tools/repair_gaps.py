# -*- coding: utf-8 -*-
"""数据完整性补采 CLI（规则 3）。

物理意图：scan_integrity 扫出的漏采段（unjustified GapRange）→ 按 missing_dates 重采
Tushare daily + adj_factor → 前复权 → merge 回 a_shares_daily 湖（dedup keep last）。

复用 sync_daily_incremental 的 _fetch_paged（按日分页 limit=500 绕过 ConnectionReset）+
前复权（price_qfq = raw × adj / latest）+ concat dedup（keep last）。

前复权基准：本次用「缺口段窗口最新 adj」（与 sync_daily_incremental 同口径），不重算
除权标的全历史 qfq 基准（memory `sync_daily_incremental:11-13` 标注的 follow-up）。

用法：
    python -m data.tools.repair_gaps --report logs/integrity.json   # 按扫描报告补
    python -m data.tools.repair_gaps --auto                         # 内部先 scan 再补
    python -m data.tools.repair_gaps --symbol 300214.SZ --auto      # 单标的诊断+补
    python -m data.tools.repair_gaps --dry-run                      # 只列段不写湖

退出码：0=成功；1=失败。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from data.integrity import GapRange, assert_safe_overwrite
# 复用 sync_daily_incremental 的 _fetch_paged（按日分页）——同源同口径，避免重复实现
from data.tools.sync_daily_incremental import _fetch_paged

logger = logging.getLogger(__name__)

LAKE = "data_lake/a_shares_daily.parquet"
PRICE_COLS = ["open", "high", "low", "close"]
OUT_COLS = ["open", "high", "low", "close", "volume"]


def repair_gaps(gaps: list[GapRange], lake_df: pd.DataFrame, pro) -> pd.DataFrame:
    """补采 unjustified gaps 的漏采日，返回 merged lake df（不写盘，调用方负责落盘）。

    Args:
        gaps: list[GapRange]（仅 suspend_justified=False 被补；停牌合法跳空跳过）。
        lake_df: 原 daily 湖（MultiIndex(date, symbol)）。
        pro: Tushare pro 接口（需有 daily/adj_factor 方法，按 trade_date 分页）。

    Returns:
        合并后的 lake df（dedup keep last，新覆盖旧）。无补采则原样返回。

    关键正确性：
    - 仅补 gap 涉及的 (symbol, date)：_fetch_paged 按日拉全市场，筛 gap_symbols + missing
      dates，避免把补采日其他 symbol 数据误并入（重复或污染）。
    - dedup keep last：补采日与 lake 已有日重叠时，新数据覆盖旧（边界日不重复）。
    """
    unjustified = [g for g in gaps if not g.suspend_justified]
    if not unjustified:
        return lake_df
    missing = sorted({d for g in unjustified for d in g.missing_dates})
    gap_symbols = {g.symbol for g in unjustified}

    # 按日分页拉 raw daily + adj_factor（复用 sync_daily_incremental._fetch_paged）
    raw_frames, adj_frames = [], []
    for td in missing:
        tdc = td.replace("-", "")
        d = _fetch_paged(pro, "daily", tdc)
        if not d.empty:
            raw_frames.append(d)
        a = _fetch_paged(pro, "adj_factor", tdc)
        if not a.empty:
            adj_frames.append(a)
    if not raw_frames:
        logger.warning("补采日 %s 全部返空（Tushare 异常/权限/或实际无数据），无新增", missing[:5])
        return lake_df

    raw = pd.concat(raw_frames, ignore_index=True)
    adj = pd.concat(adj_frames, ignore_index=True)
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], format="%Y%m%d")
    adj["trade_date"] = pd.to_datetime(adj["trade_date"], format="%Y%m%d")
    raw = raw.rename(columns={"ts_code": "symbol", "vol": "volume"})

    # 前复权：基准 = 该 symbol 在缺口段窗口最新 adj（与 sync_daily_incremental 同口径）
    merged = raw.merge(
        adj[["ts_code", "trade_date", "adj_factor"]],
        left_on=["symbol", "trade_date"], right_on=["ts_code", "trade_date"],
        how="left",
    ).drop(columns=["ts_code"], errors="ignore")
    latest_adj = (merged.sort_values(["symbol", "trade_date"])
                       .groupby("symbol")["adj_factor"].last())
    merged["latest_adj"] = merged["symbol"].map(latest_adj)
    for col in PRICE_COLS:
        if col in merged.columns:
            merged[col] = merged[col] * merged["adj_factor"] / merged["latest_adj"]

    # 组装新行 → MultiIndex(date, symbol)，筛 gap 涉及的 symbol + date
    new = merged[["trade_date", "symbol"] + OUT_COLS].copy().rename(
        columns={"trade_date": "date"})
    new["date"] = pd.to_datetime(new["date"])
    new = new.set_index(["date", "symbol"]).sort_index()
    new = new[new.index.get_level_values("symbol").isin(gap_symbols)]
    new = new[new.index.get_level_values("date").isin([pd.Timestamp(d) for d in missing])]

    if new.empty:
        logger.warning("补采段筛 symbol/date 后为空（gap 标的该日 Tushare 无数据？）")
        return lake_df

    # merge dedup keep last（新覆盖旧；与 sync_daily_incremental:132-133 同模式）
    combined = pd.concat([lake_df, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    logger.info("补采 %d 行（%d 标的 × %d 漏采日）",
                len(new), len(gap_symbols), len(missing))
    return combined


def _load_gaps_from_report(path: str) -> list[GapRange]:
    """从 scan_integrity 报告 JSON 加载 GapRange 列表。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [GapRange(symbol=g["symbol"], start=g["start"], end=g["end"],
                     missing_dates=tuple(g["missing_dates"]),
                     suspend_justified=g["suspend_justified"])
            for g in data["gaps"]]


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="数据完整性补采（规则 3）")
    ap.add_argument("--report", default=None, help="scan_integrity 报告 JSON 路径")
    ap.add_argument("--auto", action="store_true", help="内部先 scan 再补（免手传报告）")
    ap.add_argument("--symbol", default=None, help="仅补该标的（配合 --auto）")
    ap.add_argument("--since", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--lake-dir", default="data_lake")
    ap.add_argument("--dry-run", action="store_true", help="只列待补段，不写湖")
    args = ap.parse_args(argv)

    if not args.report and not args.auto:
        ap.error("需指定 --report <扫描报告> 或 --auto（内部 scan）")

    lake_path = Path(args.lake_dir) / "a_shares_daily.parquet"
    lake_df = pd.read_parquet(lake_path)

    # 获取 gaps
    if args.report:
        gaps = _load_gaps_from_report(args.report)
    else:
        from data.tools.scan_integrity import scan
        report = scan(args.lake_dir, symbol=args.symbol, since=args.since, end=args.end)
        gaps = [GapRange(symbol=g["symbol"], start=g["start"], end=g["end"],
                         missing_dates=tuple(g["missing_dates"]),
                         suspend_justified=g["suspend_justified"])
                for g in report["gaps"]]

    unjustified = [g for g in gaps if not g.suspend_justified]
    print(f"待补采：{len(unjustified)} 段漏采（{len({g.symbol for g in unjustified})} 标的）")
    if args.dry_run:
        for g in unjustified[:20]:
            print(f"  {g.symbol}  {g.start} ~ {g.end}  ({len(g.missing_dates)} 日)")
        if len(unjustified) > 20:
            print(f"  ...（共 {len(unjustified)} 段，--dry-run 仅显示前 20）")
        return 0

    from data._tushare_compat import get_pro
    pro = get_pro()
    new_lake = repair_gaps(gaps, lake_df, pro)
    delta = len(new_lake) - len(lake_df)
    # 写入前历史行数守卫（T13-A）：repair 重写全湖（覆盖写），防御 dedup/recompute bug
    # 致 new_lake 异常收缩被静默落盘。force=QUANTER_FORCE_WRITE=1 为人为重采逃生口。
    assert_safe_overwrite(str(lake_path), new_lake,
                          force=os.environ.get("QUANTER_FORCE_WRITE") == "1")
    new_lake.to_parquet(lake_path, engine="pyarrow")
    print(f"补采完成：a_shares_daily {len(lake_df)} → {len(new_lake)} 行（+{delta}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
