# -*- coding: utf-8 -*-
"""Tushare 数据湖·日频增量同步调度脚本（Task #11 债 G）。

物理意图（Why 独立脚本而非复用 sync_all_tushare）：
  sync_all_tushare.py 是**全区间重拉 + 覆盖写**编排（--years N 覆盖历史），日频调度若直接
  复用会每天重拉 N 年既慢又烧积分，且对 by=single 数据集（宏观指标/shibor）每次覆盖写
  整个区间——若按 d0+1~today 缩窗口，旧历史会被截断丢失。本脚本针对**日频增量**场景设计：
  读现有 parquet 最新日期 d0 → 只拉 [d0+1, today] 窗口新数据 → 与旧 parquet 合并去重
  （同 key 保留新）→ 落盘回原路径。

核心算法（统一 merge 范式，覆盖三类数据集）：
  1. 读旧 parquet 拿 d0（MultiIndex 时取 date 层 max；DatetimeIndex 时取 idx max；
     无时间索引的静态快照 fund_basic/margin_secs → 视为「首次」，走全量回退）。
  2. 调 sync_dataset(key, start=d0+1日, end=today) 拉新窗口——sync_dataset 内部按 cfg.by
     分发到 _sync_by_date（旧 shard 保留+新 shard append+覆盖写完整 parquet）/ _sync_single
     （覆盖写新窗口 parquet）/ _sync_by_symbol（slow 批，不在 quick 范围）。
  3. 读新 parquet → 与步骤 1 暂存的旧 parquet concat → 按 MultiIndex/索引去重保留新 →
     落盘回原路径。Why 步骤 3 必要：by=date 时 sync_dataset 已天然 merge（步骤 2 写的已是
     完整数据，本步骤 concat+去重等价），by=single 时 sync_dataset 只写了新窗口（旧数据被截断），
     必须本步骤合并还原完整历史。统一范式不依赖 cfg.by 分支判断，安全且易测。

边界与防御性：
  - parquet 不存在（首次或被删）→ 回退 sync_dataset 全量拉（用固定窗口 --years 3）。
  - cfg['_unavailable']=True → 跳过（top_list/hsgt_top10/concept 代理不可用）。
  - sync_dataset 拉到空（节假日/接口异常/限频返空）→ 跳过不覆盖旧 parquet（关键防线：防止
    临时性接口故障把旧历史覆盖成空）。
  - by=symbol 类财报数据集（fina_*）走 slow 批，**不在日频增量范围**——本脚本仅同步 quick 批。
  - --days N：限制回看窗口（防 d0 过远导致一次性拉 N 年，或 d0 异常拉爆配额）。

退出码：0=全部成功或可跳过；1=至少一个 key 失败（致命错误如积分耗尽会停整批）。

用法：
  python scripts/sync_incremental.py              # 默认 d0+1~today，无回看上限
  python scripts/sync_incremental.py --days 7     # 限制最多回看 7 天（防 d0 异常）
  python scripts/sync_incremental.py --keys moneyflow,margin  # 只同步指定 key 子集
  python scripts/sync_incremental.py --years 3    # 首次全量回退时的窗口（默认 3）
"""
from __future__ import annotations
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

# 把项目根加进 sys.path（独立脚本惯例，与 sync_all_tushare.py 同范式），保证 import config / data 可达
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # 触发 .env 加载（python-dotenv），保证 TNSKHDATA_TOKEN 注入
import pandas as pd
from config import TUSHARE_DATASETS
from data.tushare_sync import sync_dataset

# 复用 sync_all_tushare.classify() 拿 quick 批 keys（单一真相源：分批口径不在两处维护）
from scripts.sync_all_tushare import classify

# 致命错误关键词：命中则停整批（与 sync_all_tushare._FATAL 同口径，避免积分耗尽后继续烧）
_FATAL = ["积分", "频", "limit", "quota", "权限", "无权限", "没有接口"]


def _lake_path(key: str) -> str:
    """取数据集的湖 parquet 路径（TUSHARE_DATASETS[key]['lake']）。

    Why 不直接读全局 data_lake 路径：测试时会把 cfg['lake'] 重定向到 tmp_path（见
    test_tushare_sync._isolate_tushare_registry），生产环境同样依赖此字段做单一真相源。
    """
    return TUSHARE_DATASETS[key]["lake"]


def _latest_date(df: pd.DataFrame) -> pd.Timestamp | None:
    """从已落盘 parquet 的 DataFrame 提取最新日期（用于推 d0）。

    物理意图：支持两类索引形态——
      - MultiIndex(date, symbol)：取第一级 date 层的 max（股票/资金流类）。
      - DatetimeIndex：直接取 idx max（宏观指标 cn_cpi/shibor 等）。
      - 其他/无时间索引（静态快照 fund_basic/margin_secs）：返 None，调用方据此走全量回退。

    Why 显式分支而非 try-except：索引形态是数据集 cfg 的稳定契约（by=date/symbol→MultiIndex，
    single+index_mode=datetime→DatetimeIndex），分支判断可读性 > 异常驱动；返 None 语义明确
    （「无时序，需全量」），让调用方决策而非吞错。
    """
    if df is None or df.empty:
        return None
    idx = df.index
    # MultiIndex(date, symbol)：date 是第一级（tushare_sync._build_multiindex 钉死的契约）
    if isinstance(idx, pd.MultiIndex):
        # 兜底：name 可能是 ['date', 'symbol'] 或 None（理论不应出现，但保守取 level 0）
        level = idx.levels[0]
        return pd.Timestamp(level.max())
    # DatetimeIndex（宏观指标 index_mode=datetime 路径）
    if isinstance(idx, pd.DatetimeIndex):
        return pd.Timestamp(idx.max())
    # 普通对象索引（静态快照扁平 df，无时序）→ None 触发全量回退
    return None


def _merge_dedup(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """合并旧+新 DataFrame 并按索引去重，同 key 保留**新**数据。

    Why 同 key 保留新：日频增量场景下，新拉的同一 (date, symbol) 行可能是修正后的最新值
    （Tushare 偶发数据修订，如财报 ann_date 重述），保留新值确保下游读到最新口径。

    Why combine_first 不用：combine_first 语义是「old 填充 new 的空」，而我们要的是
    「new 完全覆盖同 key 的 old」——concat 后 drop_duplicates(keep='last') 才是正确范式
    （pandas 文档明确 keep='last' 保留后出现的重复行）。注意必须先 concat 时把 new 放在
    old 之后，drop_duplicates 才能保留新。

    索引兼容：old/new 索引形态必须一致（同为 MultiIndex 或同为 DatetimeIndex），否则 concat
    会创建非法的混合索引。调用方需保证——本脚本中 old/new 都从同一 lake 路径 + 同一 cfg 走出，
    形态天然一致；若旧湖历史索引漂移（罕见，可能手工编辑过），按行级 join 兜底。
    """
    # 防御：若新旧索引形态不一致（历史湖漂移），强制 reset 后按 date+symbol 去重
    if type(old.index) is not type(new.index):
        # 退化路径：reset_index 后找共同列去重，仍保留 new 优先
        old_r = old.reset_index()
        new_r = new.reset_index()
        keys = [c for c in new_r.columns if c in old_r.columns][:2]  # date+symbol 或 date 单列
        if not keys:
            # 实在没共同列（异常），保守返回 new（接受丢历史但避免写出非法 parquet）
            return new.copy()
        merged = pd.concat([old_r, new_r], ignore_index=True)
        return merged.drop_duplicates(subset=keys, keep="last").set_index(keys).sort_index()
    # 主路径：索引形态一致，concat 后按整个 index 去重保留 last（=new）
    merged = pd.concat([old, new])
    # keep='last' = 保留重复 index 中后出现的（new 在 old 之后）；取并集列（旧/新可能有列差异）
    return merged[~merged.index.duplicated(keep="last")].sort_index()


def sync_one_key(key: str, today_str: str, fallback_years: int,
                 max_days: int | None, log) -> tuple[bool, str]:
    """对单个 quick 批 key 执行增量同步。返回 (是否成功, 状态描述)。

    流程（关键边界已用注释标出）：
      [1] 跳过 _unavailable（代理不可用，与 sync_dataset 内置跳过对齐，但此处早返省一次调用）
      [2] 读旧 parquet → 推 d0；不存在 → 全量回退（首次同步或湖被删）
      [3] 推拉取窗口 [start, end]——end=today，start=d0+1日（或 max_days 限制回看）
      [4] 调 sync_dataset 拉新窗口
      [5] 读 sync_dataset 刚落盘的新 parquet；若空 → 跳过不覆盖（接口故障/节假日防护）
      [6] merge 旧+新，落盘回原路径
    """
    cfg = TUSHARE_DATASETS[key]
    # [1] _unavailable 早返：proxy 无此接口（top_list/hsgt_top10/concept），与 sync_dataset 内置
    # 跳过对齐，但此处早返省一次函数调用 + 日志层级更清晰。
    if cfg.get("_unavailable"):
        msg = f"⚠️ 跳过（不可用）：{cfg['_unavailable'][:60]}"
        print(f"[{key}] {msg}", file=log, flush=True)
        return True, msg

    lake = _lake_path(key)
    t0 = time.time()

    # [2] 读旧 parquet 推 d0
    old_df = None
    d0: pd.Timestamp | None = None
    if os.path.exists(lake):
        try:
            old_df = pd.read_parquet(lake)
            d0 = _latest_date(old_df)
        except Exception as e:
            # 旧 parquet 损坏（罕见，可能写入时被 kill）→ 视为首次走全量回退，不致命
            print(f"[{key}] ⚠️ 旧 parquet 读取失败 ({type(e).__name__}: {e})，走全量回退",
                  file=log, flush=True)
            old_df = None
            d0 = None

    # [3] 推拉取窗口
    today = pd.Timestamp(today_str)
    if d0 is None:
        # 首次或无时序（静态快照 fund_basic/margin_secs 也走此分支）：全量回退
        # Why 静态快照类全量：fund_basic/margin_secs 无时序索引，每次都应拉最新快照覆盖写，
        # 不存在「增量」概念。--years 对静态类无意义（接口不受日期约束），但走全量窗口参数
        # 不影响（sync_dataset 的 single 模式只在 cfg['date_range']=True 时消费 start/end）。
        start = (today - timedelta(days=365 * fallback_years)).strftime("%Y-%m-%d")
        end = today_str
        window_desc = f"全量回退 {start}~{end}"
    else:
        # 增量：start = d0 次日（d0 是已落盘最新日，次日起新拉）
        start_ts = d0 + timedelta(days=1)
        # --days 回看限制：若 d0 距今 > max_days（异常情况，如长期未同步或 d0 漂移），
        # 强制 start = today - max_days，避免一次性拉 N 年烧配额。Why 防御：调度可能因
        # 假期/系统故障断更数周，d0+1~today 会一次拉 2-3 周数据虽不致命但拖慢；更关键的是
        # 防止 d0=NaT/-1 等异常时间戳导致 start 落到几十年前。
        if max_days is not None:
            min_start = today - timedelta(days=max_days)
            if start_ts < min_start:
                start_ts = min_start
                window_desc = f"增量(回看限 {max_days}d) {start_ts.strftime('%Y-%m-%d')}~{today_str}"
            else:
                window_desc = f"增量 {start_ts.strftime('%Y-%m-%d')}~{today_str}"
        else:
            window_desc = f"增量 {start_ts.strftime('%Y-%m-%d')}~{today_str}"
        start = start_ts.strftime("%Y-%m-%d")
        end = today_str

    print(f"[{key}] 窗口={window_desc} d0={d0}", file=log, flush=True)

    # [4] 调 sync_dataset 拉新窗口
    # by=date 数据集 resume=True 天然增量（旧 shard 保留，新 shard append，最后覆盖写完整 parquet）；
    # by=single 数据集每次覆盖写 parquet（只含新窗口数据）——后续 [6] 的 merge 还原完整历史。
    # by=symbol 类财报不在 quick 批，sync_dataset 内 symbols 默认 None 会经 resolve_symbols 路由，
    # 但此处不应触发（classify 已分 slow 批）。若用户手动 --keys 指定 slow 批 key，仍安全走单数据集同步。
    try:
        sync_dataset(key, start, end, resume=True)
    except Exception as e:
        msg = str(e)[:140]
        dt = time.time() - t0
        print(f"[{key}] ❌ sync_dataset 异常 {dt:.0f}s {type(e).__name__}: {msg}",
              file=log, flush=True)
        return False, f"sync_dataset 异常: {msg}"

    # [5] 读 sync_dataset 刚落盘的新 parquet
    if not os.path.exists(lake):
        # sync_dataset 内部判定空（节假日/接口返空）+ single 模式 logger.warning 后直接 return
        # 不落盘——此时旧 parquet 保留（未覆盖），跳过 merge。
        dt = time.time() - t0
        print(f"[{key}] ⏭ sync_dataset 未落盘（数据为空/节假日），保留旧 parquet {dt:.0f}s",
              file=log, flush=True)
        return True, "空数据跳过"

    try:
        new_df = pd.read_parquet(lake)
    except Exception as e:
        dt = time.time() - t0
        print(f"[{key}] ❌ 新 parquet 读取失败 {dt:.0f}s {type(e).__name__}: {e}",
              file=log, flush=True)
        return False, f"新 parquet 读取失败: {e}"

    # [5.1] 新数据为空（sync_dataset 落了空 parquet，或 single 模式覆盖写后空）→ 跳过不覆盖旧
    # Why 关键防线：节假日/接口限频返空时，sync_dataset 单次模式可能落空 parquet；
    # 若不跳过 merge，会让 _merge_dedup(old, empty) 退化为只保留 old（concat 后 new 无行），
    # 虽不致命但浪费一次写盘 + 日志误导。直接早返更干净。
    if new_df.empty:
        dt = time.time() - t0
        # 旧数据存在时还原旧 parquet（防止 single 模式把旧数据覆盖成空）
        if old_df is not None and not old_df.empty:
            try:
                old_df.to_parquet(lake, engine="pyarrow")
            except Exception as e:
                print(f"[{key}] ⚠️ 还原旧 parquet 失败 {type(e).__name__}: {e}", file=log, flush=True)
        print(f"[{key}] ⏭ 新数据为空，保留旧 parquet {dt:.0f}s", file=log, flush=True)
        return True, "新数据为空跳过"

    # [6] merge 旧+新
    if old_df is not None and not old_df.empty:
        merged = _merge_dedup(old_df, new_df)
    else:
        # 首次同步无旧数据：merged=new（无需 merge，但要排序保证索引有序）
        merged = new_df.sort_index() if not isinstance(new_df.index, pd.MultiIndex) else new_df.sort_index()

    # 落盘回原路径（pyarrow 引擎与 _build_multiindex / _sync_single 一致，避免引擎混用）
    try:
        merged.to_parquet(lake, engine="pyarrow")
    except Exception as e:
        dt = time.time() - t0
        print(f"[{key}] ❌ 落盘失败 {dt:.0f}s {type(e).__name__}: {e}", file=log, flush=True)
        return False, f"落盘失败: {e}"

    dt = time.time() - t0
    old_n = 0 if old_df is None else len(old_df)
    new_n = len(new_df)
    merged_n = len(merged)
    print(f"[{key}] ✅ OK {dt:.0f}s | 旧={old_n} 新拉={new_n} 合并后={merged_n}",
          file=log, flush=True)
    return True, f"旧={old_n} 新={new_n} 合并={merged_n}"


def main():
    ap = argparse.ArgumentParser(description="Tushare 数据湖日频增量同步")
    ap.add_argument("--days", type=int, default=None,
                    help="回看窗口上限（防 d0 异常一次拉 N 年；缺省=d0+1~today 无上限）")
    ap.add_argument("--keys", default=None,
                    help="只同步指定逗号分隔 key 子集（缺省=quick 批全部）")
    ap.add_argument("--years", type=int, default=3,
                    help="首次/无时序类的全量回退窗口（默认 3 年）")
    ap.add_argument("--log", default="data_lake/.syncing/sync_incremental.log",
                    help="日志文件路径（缺省 data_lake/.syncing/sync_incremental.log）")
    args = ap.parse_args()

    today_str = datetime.today().strftime("%Y-%m-%d")
    quick, _ = classify()  # 只取 quick 批（by=date/single），slow 批（财报）不在日频范围

    # --keys 子集过滤：允许用户指定子集（用于排障/单 key 重试）
    if args.keys:
        wanted = {k.strip() for k in args.keys.split(",") if k.strip()}
        quick = [k for k in quick if k in wanted]
        missed = wanted - set(quick)
        if missed:
            # 用户指定的 key 不在 quick 批（可能误填 slow 批或拼写错），打印但不致命
            print(f"⚠️ 这些 key 不在 quick 批（不处理）：{sorted(missed)}", flush=True)

    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    print(f"=== 增量同步 START {datetime.now()} | today={today_str} | "
          f"keys={len(quick)} days={args.days} years={args.years} ===", flush=True)

    ok, fail = [], []
    with open(args.log, "a", encoding="utf-8") as log:
        print(f"\n=== START {datetime.now()} | today={today_str} | "
              f"keys={quick} days={args.days} ===", file=log, flush=True)
        for i, key in enumerate(quick, 1):
            success, msg = sync_one_key(key, today_str, args.years, args.days, log)
            if success:
                ok.append(key)
            else:
                fail.append((key, msg))
                # 致命错误（积分耗尽/权限/无接口）：停整批，避免无谓烧配额。与 sync_all_tushare 同口径。
                if any(w in msg for w in _FATAL):
                    print(f"!!! 致命错误（{msg}），停止剩余 keys", file=log, flush=True)
                    break

        print(f"\n=== DONE {datetime.now()} | OK {len(ok)} | FAIL {len(fail)} ===",
              file=log, flush=True)
        if fail:
            print("失败清单:", [k for k, _ in fail], file=log, flush=True)

    # 控制台总结
    print(f"=== DONE | OK {len(ok)} | FAIL {len(fail)} ===", flush=True)
    if fail:
        for k, m in fail:
            print(f"  FAIL {k}: {m}", flush=True)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
