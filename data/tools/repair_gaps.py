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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from data.integrity import GapRange, safe_overwrite, unjustified_subsegments
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


# ============ N1 停牌真值（2026-08-16）：unfillable sidecar ============
# 物理意图（勘探实证的配额死循环）：unjustified 段配额 [:50] 被「源恒返零行」的段
# 永占——① 2000-2004 盲区年代（Tushare daily 无该年代数据）；② suspend_d 没记到
# 的真停牌残留（2019 后稀疏化）。旧实现拉空只 warning，下轮 scan 重新进队 →
# 每轮配额全烧在永不可能补成的段上，真缺段饿死。
# 修复：拉取实证「源零行」的子段记 sidecar（symbol/range/reason/count），下轮
# scan/repair 双侧跳过；--clear-unfillable 逃生口防误标永久化（如 Tushare 单日
# 故障性返空被误标——人工核实源确有数据后清除重试）。
# 风险权衡：误标 → 该段暂不补（可逆：clear 后重试）；漏标 → 每轮空拉浪费配额
# （不可接受：这正是要治的死循环）。标记只认「当日两接口拉取完成且该标的零行」
# ——限频/超时被打断的日不记 attempted，不参与标记（无法区分故障与无数据）。


def _unfillable_path(lake_dir: str = "data_lake") -> Path:
    """unfillable sidecar 路径（与 breaker 同目录，运行时状态不入库不入 git）。"""
    return Path(lake_dir) / ".repair_unfillable.json"


def load_unfillable_entries(lake_dir: str = "data_lake") -> list[dict]:
    """读 unfillable sidecar 条目；缺失/损坏 → []。

    fail-open 理由：sidecar 是「跳过优化」不是正确性必需——读不出来退化为旧行为
    （全段重试），最坏浪费一轮配额，不丢数据。损坏只 warning 留痕（与 breaker 同口径）。
    """
    p = _unfillable_path(lake_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = data.get("entries", [])
        return entries if isinstance(entries, list) else []
    except Exception:
        logger.warning("unfillable sidecar 读失败，视为无标记（fail-open 全段重试）",
                       exc_info=True)
        return []


def save_unfillable_entries(lake_dir: str, entries: list[dict]) -> None:
    """写 unfillable sidecar（tmp + fsync + os.replace 原子落盘，G5 纪律）。

    Why 原子写（N5 · D 批加固）：并发写场景**真实存在**——classify 长跑（1h 量级）的
    周期 checkpoint 与 repair --auto 的 ``_mark_unfillable``、scan 的读取方在同一
    sidecar 上交汇（早期「无并发写场景」表述已过时，订正）。非原子 write_text 的
    半截态会被并发读者（load_unfillable_entries）读成损坏 JSON → fail-open 全部
    重试（已收标记白丢）；tmp + fsync + replace 保证读者只见「旧完整态 / 新完整态」，
    永不见半截态。崩溃窗口同理：写一半被硬杀只留 tmp 尾巴，正文件完好（tmp 残留
    无消费者，无害）。
    """
    p = _unfillable_path(lake_dir)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps({"entries": entries}, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())  # 刷盘后再 replace：防断电下「空文件已换名」的半截态
    os.replace(tmp, p)


def _merge_save_unfillable(lake_dir: str, local_entries: list[dict]) -> None:
    """落盘前 re-load 磁盘最新态 + 按 (symbol, start, end, reason) 四元组合并。

    物理意图（N5 · D 批加固，T1b 评审 Important——并发写窗口收口）：classify 全程
    在启动时 load 一次 entries、长跑期间内存态与磁盘态渐行渐远；若直接把内存态
    覆盖写回，期间 repair --auto 并发落盘的铁证条目（symbol_absent/market_empty）
    会被**静默抹掉**（下轮 scan 重新进队烧配额，标记白丢）。落盘前 re-load 磁盘 +
    键合并让两侧条目并集共存：
    - 键含 reason：同段不同分级（probe_zero_day 采样推定 vs symbol_absent 全段
      实证）语义不同级，不得互相覆盖；
    - 冲突时本 run 新条目胜出（marked_at 更新，重复标记幂等收敛到最新时间戳）。
    """
    disk = load_unfillable_entries(lake_dir)
    merged: dict[tuple, dict] = {}
    for e in disk:
        merged[(e.get("symbol"), e.get("start"), e.get("end"), e.get("reason"))] = e
    for e in local_entries:
        merged[(e.get("symbol"), e.get("start"), e.get("end"), e.get("reason"))] = e
    save_unfillable_entries(lake_dir, list(merged.values()))


def clear_unfillable(lake_dir: str = "data_lake", *, reason: str | None = None) -> bool:
    """清除 unfillable sidecar（--clear-unfillable 入口，可配 --reason 定向清除）。

    Args:
        reason: 定向清除过滤——只删该 reason 的条目（典型：只清 probe_zero_day
            采样推定的误标，**不动** symbol_absent/market_empty 全段铁证）；
            None = 整体删除（旧行为，一键重置逃生口）。
    Returns:
        是否发生了清除：整体模式 = 文件存在即删；定向模式 = 至少删掉 1 条匹配项
        （无匹配/文件缺失零副作用返 False，不动文件）。
    """
    p = _unfillable_path(lake_dir)
    if not p.exists():
        return False
    if reason is None:
        p.unlink()
        return True
    # 定向清除：fail-open 读（损坏返 []）→ 无匹配 → 不动文件。Why 不顺手整体删：
    # 文件损坏时按 reason 过滤无法安全进行，保守留置交人工核（整体逃生口仍可
    # 用不带 --reason 的 --clear-unfillable 显式触发，不静默扩大删除面）。
    entries = load_unfillable_entries(lake_dir)
    kept = [e for e in entries if e.get("reason") != reason]
    if len(kept) == len(entries):
        return False  # 无匹配：零副作用（不重写文件，保 marked_at 时间戳不动）
    if kept:
        save_unfillable_entries(lake_dir, kept)
    else:
        p.unlink()  # 清完即删整文件（不留空壳——空 sidecar 与无 sidecar 语义等同）
    return True


def unfillable_coverage(entries: list[dict]) -> dict[str, tuple[tuple[str, str], ...]]:
    """sidecar 条目 → {symbol: ((start, end), ...)} 区间分组（双侧跳过的查询结构）。

    Why 按 symbol 分组 + 区间包含判定（而非展开成日集）：不依赖交易日历即可判定
    「某日是否已标记」——sidecar 的 start/end 本就是交易日，闭区间包含即覆盖；
    分组后单标的条目通常个位数（16k 段/418 标的 ≈ 1.5 条/标的），查询近 O(1)。
    """
    by_sym: dict[str, list[tuple[str, str]]] = {}
    for e in entries:
        by_sym.setdefault(e.get("symbol", ""), []).append(
            (e.get("start", ""), e.get("end", "")))
    return {s: tuple(v) for s, v in by_sym.items()}


def repair_gaps(gaps: list[GapRange], lake_df: pd.DataFrame, pro, *,
                max_segments: int | None = None,
                lake_dir: str | None = None) -> pd.DataFrame:
    """补采 unjustified gaps 的漏采日，返回 merged lake df（不写盘，调用方负责落盘）。

    N1 停牌真值（2026-08-16）升级——修复单元从「段」改为「日级子段」：
    - 只拉 gap.unjustified_days（suspend_d 已解释的日不再白拉——省配额与限频预算）；
    - 配额 recency-first（最新子段优先）：旧 [:50] 近 symbol 序被最旧的盲区年代段
      永占（拉空只 warning 下轮重进队 = 配额死循环）；盲区=最旧，按新→旧天然绕开，
      且近期真缺段正是影响实盘识别的数据，本就该优先；
    - lake_dir 提供时启用 unfillable sidecar：双侧跳过已标记段 + 拉取实证「源零行」
      的新段记标记（见上方 sidecar 区注释）。

    Args:
        gaps: list[GapRange]（仅 suspend_justified=False 被补；停牌合法跳空跳过）。
        lake_df: 原 daily 湖（MultiIndex(date, symbol)）。
        pro: Tushare pro 接口（需有 daily/adj_factor 方法，按 trade_date 分页）。
        max_segments: 配额上限（T13-B #2，自动补采防过载）；None=不限（CLI 人工补采）。
        lake_dir: 数据湖目录——unfillable sidecar 读写锚点；None=不启用 sidecar
                  （既有单测/纯函数调用兼容）。

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
    # N1 子段展开：每个 gap 的 unjustified_days 按段内连续性拆 run——两个被解释日
    # 隔开的真缺日在物理上是两处独立漏采，配额/标记/拉取都以子段为原子单位
    # （旧构造的 GapRange 经 __post_init__ 已隐含 unjustified_days=全段，语义等价旧版）
    units: list[tuple[str, tuple[str, ...]]] = []
    for g in unjustified:
        for run in unjustified_subsegments(g):
            units.append((g.symbol, run))

    # N1 unfillable sidecar 跳过：已实证「源零行」的日不再进队（配额死循环根治）
    if lake_dir is not None:
        cov = unfillable_coverage(load_unfillable_entries(lake_dir))
        if cov:
            def _cov(sym: str, day: str) -> bool:
                return any(s <= day <= e for s, e in cov.get(sym, ()))
            _filtered = [(sym, tuple(d for d in run if not _cov(sym, d)))
                         for sym, run in units]
            _kept = [u for u in _filtered if u[1]]
            _fully_skipped = sum(1 for u in _filtered if not u[1])
            if _fully_skipped:
                logger.info("unfillable sidecar 跳过 %d 个已标记子段（%d 选中 → %d 保留，"
                            "部分缩减段继续补剩余日）", _fully_skipped, len(units), len(_kept))
            units = _kept
    if not units:
        return lake_df

    # N1 recency-first 配额：按子段末日新→旧排序再截断（盲区年代=最旧天然殿后）
    units.sort(key=lambda u: u[1][-1], reverse=True)
    if max_segments is not None and len(units) > max_segments:
        logger.warning("repair_gaps 配额截断：%d → %d 子段（recency-first，剩余下次补采）",
                       len(units), max_segments)
        units = units[:max_segments]
    missing = sorted({d for _, run in units for d in run})
    gap_symbols = {sym for sym, _ in units}

    # 按日分页拉 raw daily + adj_factor（复用 sync_daily_incremental._fetch_paged）
    raw_frames, adj_frames = [], []
    partial = False  # CR-6：拉取被异常中断，但已拉日仍要走 merge 落盘（部分补采 > 完全不补）
    # N1 unfillable 实证记录：attempted=完成两接口拉取的日（标记资格——限频/超时打断
    # 的日无法区分「故障」与「无数据」不参与标记）；market_rows=每日源全市场行数
    # （全零=盲区年代 vs 有行而个股缺席=停牌残留，reason 双向定位）
    attempted_days: set[str] = set()
    market_rows: dict[str, int] = {}
    _total_days = len(missing)
    _deadline = time.monotonic() + REPAIR_TIMEOUT  # T13-B #4：总超时边界
    for _i, td in enumerate(missing):
        # 进度上报（T13-B #4）：每 10 日 log，治 --auto 10min 无输出（可见进度不再「卡住」）
        if _total_days > 10 and _i > 0 and _i % 10 == 0:
            logger.info("repair_gaps 进度：%d/%d 日已拉（raw %d adj %d 帧）",
                        _i, _total_days, len(raw_frames), len(adj_frames))
        # 总超时（T13-B #4）：超时停止拉新段，已拉部分继续 merge 落盘（部分补采 > 完全不补）
        # 观测盲点（T6 评审 #3 注明，保持 T13-B 既有语义不改）：超时不置 partial →
        # main 记 success=True，超时打断不进熔断失败计数（与限频中断分支计数方向不同）。
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
            a = _fetch_paged(pro, "adj_factor", tdc)
            # N1：两接口都完成才记 attempted（daily 成功 adj 抛异常的日不算——拉取
            # 中断日无法区分「源无数据」与「故障」，标记必须建立在完整实证上）
            attempted_days.add(td)
            market_rows[td] = len(d)
            # T6 评审 #1（daily/adj 原子入列）：两个 fetch 都成功后才一起 append。
            # Why 不逐个 append：daily 成功即入列、adj 随后抛限频 → 该日 daily 在列
            # 而 adj 永缺 → merge how="left" 后 adj_factor=NaN → 前复权价格全 NaN
            # 落湖（P1-A 红线，sync_daily_incremental 的 adj NaN 守卫同案先例），且
            # scan 按 index 在场判「已补」永不复查。被打断日零贡献——宁缺勿 NaN。
            if not d.empty:
                raw_frames.append(d)
            if not a.empty:
                adj_frames.append(a)
        except Exception as exc:
            partial = True
            # exc_info=True（T6 评审 #2）：非限频类 bug（网络栈/解析 KeyError 等）若只
            # 落 str(exc) 会丢栈——限频原文可肉眼辨、bug 不可辨，必须留全栈供排查。
            logger.warning(
                "repair_gaps 第 %d/%d 日（%s）拉取异常：%s —— 停止拉新日，"
                "已拉 %d 日走部分补采落盘（部分补采 > 完全不补；单日 raise 会丢弃全部已拉数据）",
                _i + 1, _total_days, td, exc, _i,
                exc_info=True,
            )
            break

    def _mark_unfillable(new_pairs: set[tuple[str, str]]) -> None:
        """N1：入选子段「已尝试日全零行」→ 记 unfillable sidecar（配额死循环根治）。

        new_pairs = 本次实际落湖的 (date_str, symbol) 集——子段任一日有行即视为可补
        不标（剩余日下轮 scan 缩段重试，源侧有数据只是当日未覆盖到）。
        """
        if lake_dir is None or not units:
            return
        entries = load_unfillable_entries(lake_dir)
        existing = {(e.get("symbol"), e.get("start"), e.get("end")) for e in entries}
        added = 0
        for sym, run in units:
            att = [d for d in run if d in attempted_days]
            if not att:
                continue  # 未完成拉取的子段（超时/限频截断）：无实证，不标
            if any((d, sym) in new_pairs for d in att):
                continue  # 有行落湖 → 可补，不标
            # reason 双向定位：市场全零行=盲区年代（源无该日数据）；市场有行而该标的
            # 缺席=停牌残留（suspend_d 漏记）。混段归 market_empty（从严：疑盲区即盲区）
            reason = ("market_empty" if all(market_rows.get(d, 0) == 0 for d in att)
                      else "symbol_absent")
            key = (sym, att[0], att[-1])
            if key in existing:
                continue
            existing.add(key)
            # count = 该段不可补交易日数（marked_at 供人工审计标记时效）
            entries.append({"symbol": sym, "start": att[0], "end": att[-1],
                            "reason": reason, "count": len(att),
                            "marked_at": datetime.now().isoformat(timespec="seconds")})
            added += 1
        if added:
            # N5 · D 批加固：落盘前 re-load + 合并（与 classify checkpoint 同窗口——
            # repair 与 classify 并发跑时互不抹条目）。
            _merge_save_unfillable(lake_dir, entries)
            logger.warning("unfillable 标记 %d 子段（源零行实证，下轮 scan/repair 跳过；"
                           "--clear-unfillable 可重置）", added)

    if not raw_frames:
        logger.warning("补采日 %s 全部返空（Tushare 异常/权限/盲区年代/停牌残留），无新增",
                       missing[:5])
        _mark_unfillable(set())  # 全零行实证：入选子段全部记不可补
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
        _mark_unfillable(set())  # 市场有行而入选标的全缺席：停牌残留实证
        return _tag_partial(lake_df) if partial else lake_df

    # merge dedup keep last（新覆盖旧；与 sync_daily_incremental:132-133 同模式）
    combined = pd.concat([lake_df, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    logger.info("补采 %d 行（%d 标的 × %d 漏采日%s）",
                len(new), len(gap_symbols), len(missing),
                "，partial 部分落盘" if partial else "")
    _mark_unfillable({(pd.Timestamp(d).strftime("%Y-%m-%d"), s)
                      for d, s in new.index})
    return _tag_partial(combined) if partial else combined


# ============================================================================
# N1b 探针分类（2026-08-16）：--classify 中点采样标记（可逆、不写湖）
# ============================================================================
# 物理意图：T1 全量 --auto 严格标记需 13.8h（每 unjustified 子段全范围拉取），
# 13,767 子段收敛不动 → daemon fail-closed 闸（unjustified>0 拒跑）打不开。
# 探针降本：每子段只探「中点 1 个交易日」（daily+adj_factor 两接口），两接口完整
# 拉取后该标的行缺席 → 采样推定「源无该标的该日行情」（停牌残留/盲区年代的
# 采样版），记 sidecar reason=probe_zero_day（count=1——只实证了 1 日）；该标的
# 中点有行 → 不标（源有数据，留给正规 --auto 全段补采）。
#
# 语义分级（诚实呈现）：probe_zero_day=采样推定（中点零行），与 symbol_absent=
# 全段实证不同级；scan 报告分列 unfillable_confirmed/probed 计数。
# --clear-unfillable 可逆：误推定可整体重置（与实证标记共用逃生口）。
PROBE_ZERO_DAY = "probe_zero_day"  # sidecar reason 字面（scan 分流判据，勿改）
# classify 总超时：1.4 万子段量级全跑约 1h，REPAIR_TIMEOUT 的 30min 默认会拦腰
# 截断——探针模式单独放宽（env 可调，中断已 checkpoint 可续收）
REPAIR_CLASSIFY_TIMEOUT = int(os.getenv("REPAIR_CLASSIFY_TIMEOUT_SECONDS", "14400"))
# N5 · D 批加固（现代交易日防批量误标守卫）：盲区年代上限。2000-2004 是 Tushare
# daily 的已知盲区（全市场零行=真无数据）；2005 起现代交易日全市场应数千行——
# 该年代中点日「市场级全零」只可能是源侧故障（单日故障性返空/参数异常），绝非
# 全部标的集体停牌。若无守卫，一个故障空响应会把当日组内**全部**待探标的批量
# 误标 probe_zero_day（sidecar 一旦标记双侧跳过，误标段从此不再补采）。
_MODERN_MARKET_FLOOR = "2005-01-01"


def classify_gaps(gaps: list[GapRange], pro, *, lake_dir: str) -> dict:
    """N1b --classify 内核：unjustified 子段中点采样分类（只读源 + 写 sidecar，不写湖）。

    判定语义（Why symbol 级、且只认 daily）：探针拉的是按 trade_date 的全市场响应，
    但「零行」结论落在【该标的】——中点日市场可以很健康（数千行），唯独该标的
    在 daily 缺席（停牌残留形态）。在场证据只认 daily 行情行；adj_factor 停牌日
    也连续发布（实证见循环内注释），不能作在场证据。

    与 repair_gaps 的语义差异（Why 两套回路）：
    - repair（--auto）：全范围拉取，标记 reason 严格只认「两接口完整拉取且零行」
      的全段实证（market_empty/symbol_absent），count=段日数——慢但铁证；
    - classify（本函数）：每子段只探中点 1 日，标的缺席 → probe_zero_day 推定，
      count=1（只实证 1 日）——快但推定级，scan 报告单列不与实证混计。
    压根不接收 lake_df：物理上无写湖路径（探针只改变 sidecar，不动 parquet）。

    配额经济学：按 trade_date 拉的是全市场，同一中点交易日被多个子段共享时
    重复拉取是白耗——先按中点日分组，一日一拉、组内逐标的判定。实弹耗时按
    「唯一中点日数」而非「子段数」计；内存只驻留当日标的集（组处理完即弃）。

    Args:
        gaps: list[GapRange]（与 repair 同源；仅 suspend_justified=False 子段被探）。
        pro: Tushare pro 接口（daily/adj_factor，按 trade_date 分页）。
        lake_dir: unfillable sidecar 读写锚点（必传——classify 的唯一产出就是 sidecar）。

    Returns:
        {units, marked, nonzero, probed_days, interrupted, sidecar_entries}——
        units=过滤 sidecar 后待探子段数；marked=probe_zero_day 新标数；nonzero=
        中点有行留给 --auto 的子段数；probed_days=实探唯一交易日数；
        interrupted=是否被限频/异常/超时截断（已 checkpoint，重跑续收）；N5 起含
        现代交易日市场级全零守卫触发（源侧故障判定，该日零标记即停，见
        _MODERN_MARKET_FLOOR 注释）。
    """
    unjustified = [g for g in gaps if not g.suspend_justified]
    units: list[tuple[str, tuple[str, ...]]] = []
    for g in unjustified:
        for run in unjustified_subsegments(g):
            units.append((g.symbol, run))

    # sidecar 已标记日不再探（与 repair 跳过同源）——也是「中断重跑续收」的机制
    # 基础：已标段被 cov 过滤，重跑只探剩余段，已收标记不白费。
    cov = unfillable_coverage(load_unfillable_entries(lake_dir))
    if cov:
        def _c(sym: str, day: str) -> bool:
            return any(s <= day <= e for s, e in cov.get(sym, ()))
        units = [(sym, tuple(d for d in run if not _c(sym, d)))
                 for sym, run in units]
        units = [u for u in units if u[1]]

    # 中点分组：{日: [(symbol, run), ...]}——一日一拉，组内逐标的判定存在性
    by_day: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for sym, run in units:
        # 中点 = run[len//2]（奇数段正中，偶数段偏新一日——偏向近期证据）
        by_day.setdefault(run[len(run) // 2], []).append((sym, run))
    # 日粒度 recency-first：新中点日先探（与 repair 配额同哲学——影响实盘识别的
    # 段先定性，中断时新段已收；盲区年代日殿后，天然最后探）
    days_sorted = sorted(by_day, reverse=True)

    entries = load_unfillable_entries(lake_dir)
    existing = {(e.get("symbol"), e.get("start"), e.get("end")) for e in entries}
    marked = nonzero = probed_days = done = 0
    interrupted = False
    _total = len(units)
    _deadline = time.monotonic() + REPAIR_CLASSIFY_TIMEOUT
    for _di, day in enumerate(days_sorted):
        # 进度上报（复用 --auto 治「10min 无输出」的哲学）：每 100 中点日一报
        if len(days_sorted) > 20 and _di > 0 and _di % 100 == 0:
            logger.info("classify 进度：%d/%d 中点日（子段 %d/%d，零行标记 %d，"
                        "非零留 --auto %d）",
                        _di, len(days_sorted), done, _total, marked, nonzero)
        # 总超时：截断前 sidecar 已在周期 checkpoint 落盘（见 marked 分支），重跑续收
        if time.monotonic() > _deadline:
            logger.warning("classify 总超时（%ds）：已探 %d/%d 中点日（标记 %d），"
                           "sidecar 已 checkpoint，重跑续收",
                           REPAIR_CLASSIFY_TIMEOUT, _di, len(days_sorted), marked)
            interrupted = True
            break
        # 限频降速（复用 REPAIR_DAY_SLEEP）：相邻中点日间隔 sleep 给服务端 500/min
        # 计数窗口留余量；首日不睡——尽早暴露接口可用性（与 repair 同哲学）
        if _di > 0 and REPAIR_DAY_SLEEP > 0:
            time.sleep(REPAIR_DAY_SLEEP)
        tdc = day.replace("-", "")
        try:
            d = _fetch_paged(pro, "daily", tdc)
            a = _fetch_paged(pro, "adj_factor", tdc)
        except Exception as exc:
            # 中断不废已收标记（与 --auto 部分落盘同哲学）：checkpoint 后停止，
            # 重跑经 cov 跳过已标段续收。不进熔断计数——classify 是人工诊断
            # 回路，写 record_repair_result 会污染 --auto 补采回路的连续失败
            # 信号（3 次中断开 6h 熔断的语义只对 pipeline 自动补采成立）。
            logger.warning("classify 第 %d/%d 中点日（%s）拉取异常：%s —— "
                           "checkpoint %d 条标记后停止，重跑续收",
                           _di + 1, len(days_sorted), day, exc, marked, exc_info=True)
            interrupted = True
            break
        # N5 · D 批加固守卫：中点日属现代交易日（≥2005）且市场级全零（该日全部
        # 标的零行）→ 视为源侧故障而非真缺席：本日**不标记任何条目** + warning
        # 留痕 + 按 interrupted 处理（checkpoint 已收标记不受影响，重跑续收）。
        # Why break 而非 continue：故障性返空通常持续（限频降级/权限失效），继续
        # 探后续日只会烧配额产垃圾 warning；停跑 + 非零退出码（main 消费
        # interrupted）让人工介入排查后再续。
        if d.empty and day >= _MODERN_MARKET_FLOOR:
            logger.warning(
                "classify 中点日 %s 市场级全零（现代交易日应数千行）——判定为源侧"
                "故障而非全部标的缺席：本日 0 标记，已收标记 %d 条 checkpoint 保住，"
                "请核 Tushare daily 可用性后重跑续收", day, marked)
            interrupted = True
            break
        probed_days += 1
        # 当日标的集（组处理完即弃——全市场响应数千行，跨日驻留会吃满内存）
        daily_syms = (set(d["ts_code"]) if not d.empty and "ts_code" in d.columns
                      else set())
        for sym, run in by_day[day]:
            done += 1
            # 在场判定只认 daily（行情真值源）。Why 不看 adj_factor：实证（2026-08-16，
            # 688646.SH/300246.SZ/000838.SZ 三例 --auto 铁证 symbol_absent 日）——
            # adj_factor 对停牌股也连续发布（因子序列停牌日不中断），拿它当在场
            # 证据会把全部停牌残留判「非零」，探针一颗标不了。adj 的拉取仍保留：
            # 与 T1「两接口完整拉取」的 attempt 资格同口径（防单接口故障误判零行）。
            if sym in daily_syms:
                nonzero += 1
                continue
            key = (sym, run[0], run[-1])
            if key in existing:
                continue
            existing.add(key)
            # count=1 诚实口径：只实证中点 1 日（symbol_absent 的 count=段日数）
            entries.append({"symbol": sym, "start": run[0], "end": run[-1],
                            "reason": PROBE_ZERO_DAY, "count": 1,
                            "marked_at": datetime.now().isoformat(timespec="seconds")})
            marked += 1
            # 周期 checkpoint（每 200 条）：长跑（1h 量级）被硬杀也不丢已收标记。
            # N5 · D 批加固：落盘前 re-load + 四元组合并——长跑期间 repair --auto
            # 并发落的铁证条目不得被本进程的内存快照覆盖抹掉（T1b Important）。
            if marked % 200 == 0:
                _merge_save_unfillable(lake_dir, entries)
    # 无新增标记不写 sidecar（零副作用：非零探针轮不得凭空创建/重写标记文件；
    # 中断路径的周期 checkpoint 已保住已收标记，此处兜底落盘——同样走合并）
    if marked:
        _merge_save_unfillable(lake_dir, entries)
    logger.warning("classify 完成：标记 probe_zero_day %d 子段（非零留 --auto %d，"
                   "实探 %d 日）——probe_zero_day=采样推定（中点零行），与 "
                   "symbol_absent=全段实证不同级；--clear-unfillable 可重置",
                   marked, nonzero, probed_days)
    return {"units": _total, "marked": marked, "nonzero": nonzero,
            "probed_days": probed_days, "interrupted": interrupted,
            "sidecar_entries": len(entries)}


def _load_gaps_from_report(path: str) -> list[GapRange]:
    """从 scan_integrity 报告 JSON 加载 GapRange 列表（含 N1 日级/启发式新字段，
    旧报告缺字段时经 GapRange.__post_init__ 兜底为旧段级语义）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [GapRange(symbol=g["symbol"], start=g["start"], end=g["end"],
                     missing_dates=tuple(g["missing_dates"]),
                     suspend_justified=g["suspend_justified"],
                     unjustified_days=tuple(g.get("unjustified_days") or ()),
                     suspend_suspected=bool(g.get("suspend_suspected", False)))
            for g in data["gaps"]]


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="数据完整性补采（规则 3）")
    ap.add_argument("--report", default=None, help="scan_integrity 报告 JSON 路径")
    ap.add_argument("--auto", action="store_true", help="内部先 scan 再补（免手传报告）")
    ap.add_argument("--classify", action="store_true",
                    help="探针分类：每 unjustified 子段只拉中点 1 日，双零行记 "
                         "probe_zero_day 可逆标记（不写湖；与 --auto/--report 互斥）")
    ap.add_argument("--symbol", default=None, help="仅补该标的（配合 --auto）")
    ap.add_argument("--since", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--lake-dir", default="data_lake")
    ap.add_argument("--dry-run", action="store_true", help="只列待补段，不写湖")
    ap.add_argument("--clear-unfillable", action="store_true",
                    help="清除 unfillable sidecar 后退出（误标逃生口，不需 --auto/--report）")
    ap.add_argument("--reason", default=None,
                    help="--clear-unfillable 的定向过滤：只清该 reason 的条目（如 "
                         "probe_zero_day 采样推定），铁证标记（symbol_absent/"
                         "market_empty）保留不动")
    args = ap.parse_args(argv)

    # N1 --clear-unfillable：独立入口（人工核实源确有数据后重置标记，防误标永久化）。
    # N5 加 --reason：分类分级后的定向逃生口——误推定（probe_zero_day）可单独重置，
    # 不连坐清掉 --auto 攒下的全段铁证（铁证重攒要再烧一轮全范围拉取配额）。
    if args.clear_unfillable:
        if args.reason is None:
            if clear_unfillable(args.lake_dir):
                print(f"unfillable sidecar 已整体清除：{_unfillable_path(args.lake_dir)}")
            else:
                print("无 unfillable sidecar（无需清除）")
            return 0
        removed = sum(1 for e in load_unfillable_entries(args.lake_dir)
                      if e.get("reason") == args.reason)
        if clear_unfillable(args.lake_dir, reason=args.reason):
            kept = len(load_unfillable_entries(args.lake_dir))
            print(f"已定向清除 reason={args.reason} 条目 {removed} 条"
                  f"（保留 {kept} 条铁证/实证标记）：{_unfillable_path(args.lake_dir)}")
        else:
            print(f"无 reason={args.reason} 条目（未改动 sidecar）")
        return 0
    if args.reason is not None:
        # 显式拒配：--reason 只服务 --clear-unfillable 的定向清除——静默忽略会让
        # 「以为清了 probe 实际跑了补采」的操作事故无感知（fail-fast 优于静默）。
        ap.error("--reason 仅与 --clear-unfillable 搭配（定向清除过滤）")

    # N1b --classify：探针分类独立入口（只写 sidecar 不写湖——排在读 lake parquet
    # 之前，10M 行白载是纯浪费）。与 --auto/--report 互斥：补采与分类是不同回路，
    # 混跑会让「全段实证」与「中点推定」两类标记的产生路径不可区分。
    if args.classify:
        if args.auto or args.report:
            ap.error("--classify 与 --auto/--report 互斥（探针分类 vs 补采，不同回路）")
        # 复用 --auto 的熔断快闸：补采回路刚被限频打进 6h 冷却时，探针也停——
        # 两个回路共享同一 token 配额，冷却期硬探只会加重服务端计数
        _open, _reason = is_repair_breaker_open(args.lake_dir)
        if _open:
            print(f"探针分类跳过（{_reason}）")
            return 0
        from data.tools.scan_integrity import scan
        report = scan(args.lake_dir, symbol=args.symbol, since=args.since, end=args.end)
        gaps = [GapRange(symbol=g["symbol"], start=g["start"], end=g["end"],
                         missing_dates=tuple(g["missing_dates"]),
                         suspend_justified=g["suspend_justified"],
                         unjustified_days=tuple(g.get("unjustified_days") or ()),
                         suspend_suspected=bool(g.get("suspend_suspected", False)))
                for g in report["gaps"]]
        from data._tushare_compat import get_pro
        stats = classify_gaps(gaps, get_pro(), lake_dir=args.lake_dir)
        _tag = "（中断，sidecar 已 checkpoint，重跑续收）" if stats["interrupted"] else ""
        print(f"探针分类完成{_tag}：待探 {stats['units']} 子段 → 零行标记 "
              f"{stats['marked']}（probe_zero_day 采样推定，可逆），非零留 --auto "
              f"{stats['nonzero']}，实探 {stats['probed_days']} 日，sidecar 共 "
              f"{stats['sidecar_entries']} 条")
        # N5 · D 批加固：中断（限频/异常/超时/现代交易日市场全零守卫）→ 非零退出。
        # Why：中断=本轮没探完，0 会让 schtasks/脚本「上次运行结果」显示成功——
        # 半途而废的收敛被误当完成，daemon fail-closed 闸（unjustified>0 拒跑）
        # 打不开的根因排查被掩盖。已收标记经 checkpoint 不丢，重跑续收。
        return 1 if stats["interrupted"] else 0

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
                         suspend_justified=g["suspend_justified"],
                         unjustified_days=tuple(g.get("unjustified_days") or ()),
                         suspend_suspected=bool(g.get("suspend_suspected", False)))
                for g in report["gaps"]]

    unjustified = [g for g in gaps if not g.suspend_justified]
    _n_subsegs = sum(len(unjustified_subsegments(g)) for g in unjustified)
    print(f"待补采：{len(unjustified)} 段（{len({g.symbol for g in unjustified})} 标的，"
          f"日级拆分 {_n_subsegs} 子段，recency-first 配额 {MAX_REPAIR_SEGMENTS}）")
    if args.dry_run:
        # 按末日新→旧展示（与 repair 选择序一致：最新真缺段优先进入配额）
        for g in sorted(unjustified, key=lambda g: g.end, reverse=True)[:20]:
            print(f"  {g.symbol}  {g.start} ~ {g.end}  "
                  f"（缺 {len(g.missing_dates)} 日，真缺 {len(g.unjustified_days)} 日）")
        if len(unjustified) > 20:
            print(f"  ...（共 {len(unjustified)} 段，--dry-run 仅显示最新前 20）")
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
        # auto 模式传配额；--report 人工模式不传（保原签名，兼容外部 mock）。
        # lake_dir 双模式都传：unfillable sidecar 跳过/标记对人工补采同样生效
        # （人工补采更不该把时间烧在已实证不可补的段上）。
        if auto_mode:
            new_lake = repair_gaps(gaps, lake_df, pro, max_segments=MAX_REPAIR_SEGMENTS,
                                   lake_dir=args.lake_dir)
        else:
            new_lake = repair_gaps(gaps, lake_df, pro, lake_dir=args.lake_dir)
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
