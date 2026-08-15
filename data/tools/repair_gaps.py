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
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from data.integrity import GapRange, safe_overwrite
# 复用 sync_daily_incremental 的 _fetch_paged（按日分页）——同源同口径，避免重复实现
from data.tools.sync_daily_incremental import _fetch_paged

logger = logging.getLogger(__name__)

LAKE = "data_lake/a_shares_daily.parquet"
PRICE_COLS = ["open", "high", "low", "close"]
OUT_COLS = ["open", "high", "low", "close", "volume"]
# repair 总超时（T13-B #4）：超时停止拉新段，已拉部分继续 merge 落盘（部分补采 > 完全不补）。
# 治 --auto 10min 无输出：有进度 log + 有超时边界，不再「卡住不知在干嘛」。
REPAIR_TIMEOUT = int(os.getenv("REPAIR_TIMEOUT_SECONDS", "1800"))  # 30min 默认（覆盖全市场多日补采）

# ============ T13-B #2：配额 + 熔断（自动补采保护，仅 pipeline 自动 repair 用）============
# 配额防单次过载；熔断防连续失败（含代理失败）雪崩。CLI main（人工）不限额/不熔断（人决策）。
MAX_REPAIR_SEGMENTS = int(os.getenv("REPAIR_MAX_SEGMENTS", "50"))           # 单次最多补 50 段
REPAIR_FAILURE_THRESHOLD = int(os.getenv("REPAIR_FAILURE_THRESHOLD", "3"))  # 连续 3 次失败熔断
REPAIR_RECOVERY_HOURS = float(os.getenv("REPAIR_RECOVERY_HOURS", "6"))      # 熔断 6h

# CR-6 限频降速：相邻漏采日之间的强制间隔（秒）。实锤根因（repair_auto.log 25 连败）：
# Tushare 服务端 500/min 计数窗口与客户端令牌桶错位——令牌桶允许「合规」瞬时突发，
# 服务端却按自己的滚动窗口掐表 → 客户端自认未超限、服务端已判频率超限。日间隔 sleep
# 把按日分组的分页请求在时间轴上摊开，给服务端计数窗口留恢复余量（降速换通过率）。
REPAIR_DAY_SLEEP = float(os.getenv("REPAIR_DAY_SLEEP", "1.5"))


def _tag_partial(df: pd.DataFrame) -> pd.DataFrame:
    """在返回 df 的 attrs 上打 partial 标记（CR-6：拉取被异常中断，已拉部分待落盘）。

    Why attrs 而非改返回签名：repair_gaps 的返回值已被 main / 既有单测 / 外部 mock 按
    「单 df」消费，改成 tuple 会破坏全部调用方；attrs 是 pandas 官方的元数据随行通道，
    main 紧随 repair_gaps 读取（中间无其他 pandas 运算，不存在 attrs 丢失面）。
    早退分支会连带在调用方传入的 lake_df 上打标（attrs 仅元数据，不改数据体，无害）。
    """
    df.attrs["partial"] = True
    return df


def _repair_breaker_path(lake_dir: str = "data_lake") -> Path:
    """熔断 sidecar 路径（与 freshness baseline 同目录，运行时状态不入库）。"""
    return Path(lake_dir) / ".repair_breaker.json"


def is_repair_breaker_open(lake_dir: str = "data_lake", *, now: float | None = None) -> tuple[bool, str]:
    """查熔断是否开启（pipeline 自动补采前置检查）。

    Returns:
        (open, reason)：open=True 熔断中（跳过自动 repair + 告警）；False 可补采。
    """
    bp = _repair_breaker_path(lake_dir)
    if not bp.exists():
        return False, ""
    try:
        data = json.loads(bp.read_text(encoding="utf-8"))
        open_until = data.get("open_until", 0)
        _now = now if now is not None else time.time()
        if open_until and _now < open_until:
            return True, f"熔断中（连续 {data.get('fail_count', 0)} 次失败，{REPAIR_RECOVERY_HOURS}h 后恢复）"
    except Exception:
        logger.warning("repair 熔断 sidecar 读失败，视为未熔断（fail-open 补采）", exc_info=True)
    return False, ""


def record_repair_result(success: bool, lake_dir: str = "data_lake", *, now: float | None = None) -> None:
    """记录自动补采结果：成功清计数；失败计数+1，超阈值熔断。

    物理意图（blueprint §2.3）：连续失败（含代理失败）→ 熔断暂停，绝不吞失败当成功。
    """
    bp = _repair_breaker_path(lake_dir)
    try:
        data = json.loads(bp.read_text(encoding="utf-8")) if bp.exists() else {}
    except Exception:
        data = {}
    _now = now if now is not None else time.time()
    if success:
        data = {"fail_count": 0, "open_until": 0}
    else:
        fail_count = data.get("fail_count", 0) + 1
        data["fail_count"] = fail_count
        if fail_count >= REPAIR_FAILURE_THRESHOLD:
            data["open_until"] = _now + REPAIR_RECOVERY_HOURS * 3600
            logger.warning("repair 熔断开启：连续 %d 次失败，暂停 %gh", fail_count, REPAIR_RECOVERY_HOURS)
    bp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def repair_gaps(gaps: list[GapRange], lake_df: pd.DataFrame, pro, *,
                max_segments: int | None = None) -> pd.DataFrame:
    """补采 unjustified gaps 的漏采日，返回 merged lake df（不写盘，调用方负责落盘）。

    Args:
        gaps: list[GapRange]（仅 suspend_justified=False 被补；停牌合法跳空跳过）。
        lake_df: 原 daily 湖（MultiIndex(date, symbol)）。
        pro: Tushare pro 接口（需有 daily/adj_factor 方法，按 trade_date 分页）。
        max_segments: 配额上限（T13-B #2，自动补采防过载）；None=不限（CLI 人工补采）。

    Returns:
        合并后的 lake df（dedup keep last，新覆盖旧）。无补采则原样返回。
        CR-6：拉取被异常中断（频率超限/网络断）时不再 raise，已拉日照常 merge 返回，
        并在返回 df 的 attrs 打 ``partial=True`` 标记（部分补采 > 完全不补；230/350
        白拉教训——单日异常 raise 会丢弃全部已拉数据）。

    关键正确性：
    - 仅补 gap 涉及的 (symbol, date)：_fetch_paged 按日拉全市场，筛 gap_symbols + missing
      dates，避免把补采日其他 symbol 数据误并入（重复或污染）。
    - dedup keep last：补采日与 lake 已有日重叠时，新数据覆盖旧（边界日不重复）。
    """
    unjustified = [g for g in gaps if not g.suspend_justified]
    if not unjustified:
        return lake_df
    # 配额截断（T13-B #2）：自动补采单次最多 max_segments 段（防过载）；None=不限（CLI 人工）
    if max_segments is not None and len(unjustified) > max_segments:
        logger.warning("repair_gaps 配额截断：%d → %d 段（剩余下次补采）",
                       len(unjustified), max_segments)
        unjustified = unjustified[:max_segments]
    missing = sorted({d for g in unjustified for d in g.missing_dates})
    gap_symbols = {g.symbol for g in unjustified}

    # 按日分页拉 raw daily + adj_factor（复用 sync_daily_incremental._fetch_paged）
    raw_frames, adj_frames = [], []
    partial = False  # CR-6：拉取被异常中断，但已拉日仍要走 merge 落盘（部分补采 > 完全不补）
    _total_days = len(missing)
    _deadline = time.monotonic() + REPAIR_TIMEOUT  # T13-B #4：总超时边界
    for _i, td in enumerate(missing):
        # 进度上报（T13-B #4）：每 10 日 log，治 --auto 10min 无输出（可见进度不再「卡住」）
        if _total_days > 10 and _i > 0 and _i % 10 == 0:
            logger.info("repair_gaps 进度：%d/%d 日已拉（raw %d adj %d 帧）",
                        _i, _total_days, len(raw_frames), len(adj_frames))
        # 总超时（T13-B #4）：超时停止拉新段，已拉部分继续 merge 落盘（部分补采 > 完全不补）
        if time.monotonic() > _deadline:
            logger.warning("repair_gaps 总超时（%ds）：已拉 %d/%d 日，部分补采落盘",
                           REPAIR_TIMEOUT, _i, _total_days)
            break
        # CR-6 限频降速：日间隔 sleep 给 Tushare 服务端 500/min 计数窗口留余量
        # （首日不睡：不延迟第一个请求，尽早暴露接口可用性）。sleep 计入总超时预算。
        if _i > 0 and REPAIR_DAY_SLEEP > 0:
            time.sleep(REPAIR_DAY_SLEEP)
        tdc = td.replace("-", "")
        # CR-6 部分落盘（与上方超时 break 分支同语义）：单日拉取异常（频率超限/网络断/
        # 代理失败）不再直接 raise——旧实现把已拉的 230/350 日全部丢弃白拉（repair_auto.log
        # 实锤 25 连败熔断循环根因：一次限频 = 一轮全废）。部分补采 > 完全不补：
        # warning 留痕 + break 停止拉新日，已拉日继续走下方 merge 落盘路径；
        # partial 标记透传 main → 记熔断失败计数（连续 3 次中断才熔断退避，非单次雪崩）。
        try:
            d = _fetch_paged(pro, "daily", tdc)
            if not d.empty:
                raw_frames.append(d)
            a = _fetch_paged(pro, "adj_factor", tdc)
            if not a.empty:
                adj_frames.append(a)
        except Exception as exc:
            partial = True
            logger.warning(
                "repair_gaps 第 %d/%d 日（%s）拉取异常：%s —— 停止拉新日，"
                "已拉 %d 日走部分补采落盘（部分补采 > 完全不补；单日 raise 会丢弃全部已拉数据）",
                _i + 1, _total_days, td, exc, _i,
            )
            break
    if not raw_frames:
        logger.warning("补采日 %s 全部返空（Tushare 异常/权限/或实际无数据），无新增", missing[:5])
        # 第 1 日即被打断：无数据可落，但 partial 仍要透传（main 记失败计数，连续 3 次熔断）
        return _tag_partial(lake_df) if partial else lake_df

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
        return _tag_partial(lake_df) if partial else lake_df

    # merge dedup keep last（新覆盖旧；与 sync_daily_incremental:132-133 同模式）
    combined = pd.concat([lake_df, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    logger.info("补采 %d 行（%d 标的 × %d 漏采日%s）",
                len(new), len(gap_symbols), len(missing),
                "，partial 部分落盘" if partial else "")
    return _tag_partial(combined) if partial else combined


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

    # T13-B #2：--auto 自动模式（pipeline 子进程触发）用配额 + 熔断；--report 人工模式不限。
    auto_mode = args.auto and not args.report
    if auto_mode:
        _open, _reason = is_repair_breaker_open(args.lake_dir)
        if _open:
            print(f"自动补采跳过（{_reason}）")
            return 0
    from data._tushare_compat import get_pro
    pro = get_pro()
    try:
        # auto 模式传配额；--report 人工模式不传（保原签名，兼容外部 mock）
        if auto_mode:
            new_lake = repair_gaps(gaps, lake_df, pro, max_segments=MAX_REPAIR_SEGMENTS)
        else:
            new_lake = repair_gaps(gaps, lake_df, pro)
        # CR-6：读 partial 标记（拉取被限频/异常中断，但已拉部分已 merge 完待落盘）。
        # 标记用 ASCII「partial」——log 经 GBK/UTF8 多道转码仍可 grep（pipeline 侧可识别）。
        partial = bool(new_lake.attrs.get("partial", False))
        delta = len(new_lake) - len(lake_df)
        # 写入前历史行数守卫 + 原子落盘（T13-A 守卫 + G5 原子写）：safe_overwrite 内部完成
        # 「守卫 + tmp + fsync + os.replace」原子写入，调用方不再紧跟 to_parquet（防半截损坏）。
        # repair 重写全湖（覆盖写），防御 dedup/recompute bug 致 new_lake 异常收缩被静默落盘。
        safe_overwrite(str(lake_path), new_lake)
        if auto_mode:
            # CR-6 熔断计数方向：部分补采 = 数据已落盘不丢，但「本轮被限频打断」是失败信号
            # ——记失败计数（连续 3 次中断 → 熔断 6h 退避，给服务端限频窗口恢复余量）。
            # 与旧「单次中断即整轮 raise 雪崩」的区别：中断不废已拉数据，熔断只看连续性。
            record_repair_result(success=not partial, lake_dir=args.lake_dir)
        _tag = "（partial，拉取中断部分落盘）" if partial else ""
        print(f"补采完成{_tag}：a_shares_daily {len(lake_df)} → {len(new_lake)} 行（+{delta}）")
        return 0
    except Exception:
        # 自动模式记熔断失败计数（连续 K 次熔断）；人工模式仅抛
        if auto_mode:
            record_repair_result(success=False, lake_dir=args.lake_dir)
        raise


if __name__ == "__main__":
    sys.exit(main())
