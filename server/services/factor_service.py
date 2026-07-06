# -*- coding: utf-8 -*-
"""层级二·因子服务：注册表反射 + IC/IR 衰减计算。

职责：
- list_factors / get_detail：反射 FactorLoader，组装 drill-down（关联数据集 + 引用策略）。
- compute_ic_decay：对 grid_computable 因子，跨多个持有期算 IC 衰减曲线 + 月度×horizon 热力图。

拷问三连（已显式处置）：
- 前视偏差：远期收益用 returns.shift(-h)，因子值不重算（仅当日横截面信息），无未来泄漏。
- NaN/Inf 防线：委托 FactorAnalyzer.compute_ic 内部 dropna + std==0 中性化，绝不向上抛 NaN。
- 性能：universe 默认解析为 daily_active 活跃池（~50 只，AKSHARE_CONFIG.active_pool_size），
  避免 full-market daily（数千标的）拖垮 IC 计算；显式 universe 上限 80 防滥用。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from factors.base import FactorEntry, FactorLoader
from factors.analyzer import FactorAnalyzer

logger = logging.getLogger(__name__)

# IC 衰减默认持有期（交易日）：覆盖 1 日（短期预测力）到 20 日（约月度）
_DEFAULT_HORIZONS = (1, 3, 5, 10, 20)
# universe 上限（防前端传入巨量标的拖垮计算）
_UNIVERSE_LIMIT = 80
# 默认活跃池 lake key（数据最完整的 ~50 只，匹配 explorer dynamic_top50 语义）
_ACTIVE_LAKE = "daily_active"


def list_factors(loader: FactorLoader) -> List[Dict[str, Any]]:
    """反射全部因子摘要（透传 loader.list()）。"""
    return loader.list()


def get_detail(loader: FactorLoader, strategy_loader: Any, name: str) -> Optional[Dict[str, Any]]:
    """组装单因子 drill-down：元数据 + 关联数据集 + 引用策略。

    引用策略：扫描 strategy_loader 注册表，读每个策略类的 composition（Layer 3 接入的
    ClassVar，声明依赖的因子名列表）；Layer 3 未接入前 composition 缺失则返回空列表（诚实）。
    """
    try:
        entry: FactorEntry = loader.get(name)
    except KeyError:
        return None
    m = entry.meta
    summary = {
        "name": m.name, "label": m.label, "category": m.category, "author": m.author,
        "status": m.status, "input_kind": m.input_kind, "dataset": m.dataset,
        "description": m.description, "grid_computable": m.grid_computable,
        "default_params": dict(m.default_params),
    }
    # 关联数据集：因子声明的 dataset（单值，未来可扩多源）；列表化便于前端多标签展示
    datasets = [m.dataset] if m.dataset else []

    # 引用策略：扫描 strategy_loader 各策略的 composition ClassVar
    referenced_by: List[Dict[str, str]] = []
    if strategy_loader is not None:
        try:
            registry = getattr(strategy_loader, "_registry", {}) or {}
            for sname, scls in registry.items():
                comp = getattr(scls, "composition", None)
                # composition 形如 {"factors": ["cross_sectional_momentum", ...], ...}
                if isinstance(comp, dict):
                    facs = comp.get("factors") or []
                elif isinstance(comp, (list, tuple)):
                    facs = comp
                else:
                    facs = []
                if name in facs:
                    referenced_by.append({
                        "name": sname,
                        "label": getattr(scls, "label", sname),
                    })
        except Exception as exc:
            logger.debug("引用策略扫描失败(因子=%s): %s", name, exc)

    return {
        "summary": summary,
        "datasets": datasets,
        "referenced_by": referenced_by,
    }


def _resolve_universe(reader: Any, universe: Optional[List[str]]) -> Tuple[List[str], str]:
    """解析 universe → (symbols, lake_key)。

    - 显式标的（非 dynamic_top50 别名）→ 取前 _UNIVERSE_LIMIT 个，lake 取活跃池（若有）否则 daily。
    - 缺省/dynamic_top50 → 活跃池全部标的（~50 只）。
    返回空列表表示无可用标的（调用方据此返回 ok=False）。
    """
    active_loaded = _ACTIVE_LAKE in getattr(reader, "_lakes", {})
    daily_loaded = "daily" in getattr(reader, "_lakes", {})
    default_lake = _ACTIVE_LAKE if active_loaded else ("daily" if daily_loaded else "")

    if universe:
        explicit = [u for u in universe if u and u not in ("dynamic_top50", "dynamic_top100", "all")]
        if explicit:
            return explicit[:_UNIVERSE_LIMIT], default_lake
    if not default_lake:
        return [], ""
    df = reader._lakes[default_lake]
    idx = df.index
    if "symbol" not in getattr(idx, "names", []):
        return [], default_lake
    syms = list(dict.fromkeys(idx.get_level_values("symbol").tolist()))  # 保序去重
    return syms[:_UNIVERSE_LIMIT], default_lake


def _build_ohlcv_panel(reader: Any, symbols: List[str], start: str, end: str,
                       lake: str) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """从指定湖拼 close/high/low 面板（index=date, columns=symbol）。

    缺列（如湖只有 close）对应面板返空 DataFrame；调用方按需用。
    """
    pc, ph, pl = [], [], []
    for sym in symbols:
        ts = reader.get_timeseries(sym, start, end, lake=lake)
        if ts.empty:
            continue
        if "close" in ts:
            pc.append(ts["close"].rename(sym))
        if "high" in ts:
            ph.append(ts["high"].rename(sym))
        if "low" in ts:
            pl.append(ts["low"].rename(sym))
    close = pd.concat(pc, axis=1).sort_index() if pc else pd.DataFrame()
    high = pd.concat(ph, axis=1).sort_index() if ph else pd.DataFrame()
    low = pd.concat(pl, axis=1).sort_index() if pl else pd.DataFrame()
    return close, high, low


def compute_ic_decay(loader: FactorLoader, name: str, start: str, end: str,
                     universe: Optional[List[str]] = None,
                     horizons: Optional[List[int]] = None) -> Dict[str, Any]:
    """对 grid_computable 因子计算 IC 衰减曲线 + 月度×horizon 热力图。

    非面板因子（lake_series/cross_section/set）→ ok=False 并说明原因（不支持 IC 网格）。
    数据缺失/计算异常 → ok=False 并回填 reason，绝不向上抛（前端友好降级）。
    """
    try:
        entry: FactorEntry = loader.get(name)
    except KeyError:
        return {"ok": False, "name": name, "reason": f"未注册的因子: {name}"}
    meta = entry.meta

    if not meta.grid_computable:
        return {"ok": False, "name": name, "label": meta.label,
                "reason": f"因子为 {meta.input_kind} 型，非横截面面板因子，不支持 IC 衰减分析"}

    # 延迟 import DataLakeReader：避免模块级耦合 + 触发单例初始化
    from data.lake_reader import DataLakeReader
    reader = DataLakeReader.get_instance()
    if not reader.loaded:
        return {"ok": False, "name": name, "label": meta.label, "reason": "数据湖未加载"}

    syms, lake = _resolve_universe(reader, universe)
    if not syms:
        return {"ok": False, "name": name, "label": meta.label, "reason": "universe 解析为空（活跃池/日线湖未加载）"}

    close, high, low = _build_ohlcv_panel(reader, syms, start, end, lake)
    if close.empty:
        return {"ok": False, "name": name, "label": meta.label, "reason": "universe 无可用 close 数据"}
    returns = close.pct_change()

    # 按输入契约计算因子值
    try:
        if meta.input_kind == "returns_panel":
            factor = entry.func(returns, **meta.default_params)
        else:  # ohlcv_panel：需要 high/low/close
            factor = entry.func(returns, high, low, close, **meta.default_params)
    except Exception as exc:
        logger.warning("因子 %s 计算异常: %s", name, exc)
        return {"ok": False, "name": name, "label": meta.label,
                "reason": f"因子计算异常: {type(exc).__name__}: {exc}"}

    hlist = list(horizons) if horizons else list(_DEFAULT_HORIZONS)
    analyzer = FactorAnalyzer()
    decay: List[Dict[str, Any]] = []
    ic_by_horizon: Dict[int, pd.Series] = {}

    for h in hlist:
        # 远期收益 shift(-h)：用「未来 h 日收益」检验「当日因子值」的预测力（因果合法：
        # 因子值取当日横截面，远期收益取未来，二者秩相关即 IC，无前视污染）
        fwd = returns.shift(-h)
        ic_out = analyzer.compute_ic(factor, fwd)
        ic_by_horizon[h] = ic_out["ic_series"]
        decay.append({
            "horizon": h,
            "ic_mean": ic_out["ic_mean"],
            "ic_ir": ic_out["ic_ir"],
            "t_stat": ic_out["t_stat"],
        })

    # 月度 × horizon IC 矩阵（ECharts heatmap 直消费：[month_idx, horizon_idx, ic]）
    month_set = set()
    for s in ic_by_horizon.values():
        for dt in s.index:
            month_set.add(pd.Timestamp(dt).strftime("%Y-%m"))
    months = sorted(month_set)
    month_idx = {m: i for i, m in enumerate(months)}
    hidx = {h: i for i, h in enumerate(hlist)}
    cells: List[List[Any]] = []
    for h in hlist:
        s = ic_by_horizon[h]
        for dt, val in s.items():
            if val is None or (isinstance(val, float) and (np.isnan(val) or np.isinf(val))):
                continue
            ym = pd.Timestamp(dt).strftime("%Y-%m")
            cells.append([month_idx[ym], hidx[h], float(val)])

    return {
        "ok": True,
        "name": name,
        "label": meta.label,
        "n_symbols": len(syms),
        "decay": decay,
        "heatmap": {"months": months, "horizons": hlist, "data": cells},
    }
