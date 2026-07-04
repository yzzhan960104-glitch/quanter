"""Celery 实例 + 因子网格任务。

设计：单 Redis broker/backend；不引 beat。worker 核心调 FactorAnalyzer/exploratory_momentum，
结果落 reports/explorer/{task_id}.json。

关键工程取舍（Why）：
- Celery app 为模块级单例，但实例化仅记录 broker_url，不在此刻连 Redis（lazy）；
  因此开发机/CI 无 Redis 时仍可正常 import 本模块、被 explorer 路由引用——
  Redis 真正不可用只会在 `.delay()` 时显式抛 redis.ConnectionError，
  届时由 explorer 路由捕获并降级线程池，绝不阻断主流程。
"""
from __future__ import annotations

import json
import os

import pandas as pd  # impl 拼截面面板用，顶部显式 import（不用延迟加载/noqa）
from celery import Celery

from config import CELERY_CONFIG

# Why Celery(..., broker/backend)：单 Redis 同时承担消息中间件与结果后端，
# 极简拓扑、运维单点；实例化不建连接（lazy），保证无 Redis 也可 import。
celery_app = Celery("quanter",
                    broker=CELERY_CONFIG["broker_url"],
                    backend=CELERY_CONFIG["broker_url"])
celery_app.conf.task_default_queue = CELERY_CONFIG["queue"]


def run_factor_grid_impl(spec: dict) -> dict:
    """网格计算实现（同步纯函数，可被 worker 或线程池调用）。

    spec 形如 {factor, universe, start, end}。
    本实现以 DataLakeReader 为数据源、FactorAnalyzer 为评估器；
    数据源缺失时返回空结果（不抛）——保证降级路径在任何环境下都安全可调用。

    Why 函数内 import DataLakeReader/FactorAnalyzer：
    - 这些重模块在 import 期会触发自身单例/配置读取，延迟到调用时才发生，
      避免在 celery_app 模块级形成对数据湖/因子库的硬 import 时序耦合；
    - 函数内 import 在 Python 中有 LRU 字节码缓存，反复调用无显著开销。
    """
    import numpy as np
    from data.lake_reader import DataLakeReader
    from factors.analyzer import FactorAnalyzer
    from factors.exploratory_momentum import cross_sectional_momentum

    reader = DataLakeReader.get_instance()
    # 离线模式（无 parquet）→ 直接返回空结果，绝不抛异常打断 worker/线程池
    if not reader.loaded:
        return {"ok": False, "reason": "数据湖未加载"}
    # 收集 universe 时序，拼成截面 returns 面板
    pieces = []
    for sym in spec.get("universe", []):
        ts = reader.get_timeseries(sym, spec["start"], spec["end"])
        if not ts.empty:
            pieces.append(ts["close"].rename(sym))
    if not pieces:
        # universe 全部无数据（如停牌/标的不在湖中）→ 安全返回，不抛
        return {"ok": False, "reason": "universe 无可用数据"}
    panel = pd.concat(pieces, axis=1).sort_index()
    returns = panel.pct_change()
    factor = cross_sectional_momentum(returns, window=20)
    fwd = returns.shift(-1)

    analyzer = FactorAnalyzer()
    ic_out = analyzer.compute_ic(factor, fwd)
    frac = analyzer.fractile_analysis(factor, fwd, n_groups=5)

    # IC 时序 + 日期（dropna 防 NaN 进直方图）
    ic_series = ic_out["ic_series"].dropna()
    dates = [d.strftime("%Y-%m-%d") for d in ic_series.index]
    ic_list = [float(v) for v in ic_series.values]

    # 分层累计净值：每组 (1+r).cumprod() 后归一起点 1.0；LS = Q5 累计 - Q1 累计
    group_returns = frac["group_returns"]   # dict[g, Series of 远期收益]
    n_groups = 5
    quantile_nav: dict = {}
    group_cum: dict = {}
    for g in range(n_groups):
        s = group_returns.get(g, pd.Series(dtype=float)).dropna()
        if s.empty:
            group_cum[g] = pd.Series(dtype=float)
            continue
        cum = (1.0 + s).cumprod()
        cum = cum / cum.iloc[0] if cum.iloc[0] != 0 else cum   # 起点归一 1.0
        group_cum[g] = cum
        quantile_nav[f"Q{g + 1}"] = [float(v) for v in cum.values]
    # 多空 Alpha：Q5 累计 - Q1 累计（对齐到 Q5 索引，Q1 缺失前向填充）
    q5 = group_cum.get(n_groups - 1, pd.Series(dtype=float))
    q1 = group_cum.get(0, pd.Series(dtype=float))
    if not q5.empty and not q1.empty:
        ls = (q5 - q1.reindex(q5.index).ffill()).fillna(0.0)
        quantile_nav["LS"] = [float(v) for v in ls.values]
    else:
        quantile_nav["LS"] = []

    # IC 直方图（bin 数自适应样本量；样本过少返空边界保护前端）
    if len(ic_list) >= 2:
        counts, edges = np.histogram(ic_list, bins=min(20, max(5, len(ic_list) // 2)))
        ic_hist = {"bin_edges": [float(x) for x in edges], "counts": [int(c) for c in counts]}
    else:
        ic_hist = {"bin_edges": [], "counts": []}

    return {
        "ok": True,
        "factor": spec.get("factor", ""),
        "dates": dates,
        "ic_series": ic_list,
        "ic_mean": float(ic_out["ic_mean"]),
        "ic_ir": float(ic_out["ic_ir"]),
        "t_stat": float(ic_out["t_stat"]),
        "quantile_nav": quantile_nav,
        "ic_hist": ic_hist,
    }


@celery_app.task(name="explorer.run_factor_grid")
def run_factor_grid(spec: dict) -> str:
    """Celery 任务入口：跑网格、落盘、返回结果摘要路径。

    Why 显式落盘 reports/explorer/{task_id}.json：
    - Celery backend 只存可序列化结果字符串，因子评估产物（IC 序列、分层收益）
      体量较大且需后续被报告/前端复用，落盘后通过返回路径解耦结果存储与任务队列；
    - 任务用 request.id 作为文件名，天然唯一、可被 GET /result/{task_id} 反查。
    """
    result = run_factor_grid_impl(spec)
    task_dir = "reports/explorer"
    os.makedirs(task_dir, exist_ok=True)
    out_path = os.path.join(task_dir, f"{run_factor_grid.request.id}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, default=str)
    return out_path
