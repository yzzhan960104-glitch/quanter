# -*- coding: utf-8 -*-
"""price_data 装配 + cfg 合并的模块级函数（Step4e 抽自 caisen/facade.py）。

物理意图（Step4e 反向债收口）：
    原 ``CaisenFacade._load_price_data`` / ``CaisenFacade._merge_cfg`` 是 facade 实例方法，
    但二者【不使用任何 self 状态】（_merge_cfg 完全无 self；_load_price_data 仅用模块级
    logger）。Step4e 把二者的【纯逻辑】抽到本模块（data/price_loader.py）成为模块级函数，
    消除 execution/replay_worker.py 对 server.services.caisen_service 的反向依赖。

Layer2 解耦·Task 1.3（caisen 形态退役）：
    caisen/facade.py 已删（caisen 形态整体退役）。本模块保留为 generic 数据装配工具：
      - load_price_data：策略中立（从 data_lake 装配时序），所有策略共用。
      - merge_cfg：按 strategy_name 选 pydantic schema 做 cfg_override 校验（Task 1.3
        从硬编码 StrategyConfig 改为 generic；caisen 形态退役后仅颈线法 NecklineConfig）。

定位选择（为什么放 data/）：
    load_price_data 的物理职责是「从 data_lake 装配时序」，依赖 data.lake_reader +
    config.LAKE_CONFIG，归属 data/ 层最自然。merge_cfg 是 cfg 装配工具，随 load_price_data
    同搬（worker 二者一起 import）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import pandas as pd

# 注：merge_cfg 的 schema import 故意延迟到函数体内（不放模块顶）。原因：
# data.price_loader 位于 data/ 包（与 strategies/ 平级），模块顶 import 任一策略 schema
# 可能触发 strategies 包初始化链的反向依赖。延迟到函数体内打破潜在循环——首次调
# merge_cfg 时各包已完全初始化，schema 可安全取到。load_price_data 本身不依赖 schema，
# 不受影响。
#
# Task 1.3（Layer2 解耦·caisen 形态退役）：原 merge_cfg 硬编码 caisen StrategyConfig
# 做 cfg_override 校验。caisen 形态退役后 StrategyConfig 已删，merge_cfg 改为按
# strategy_name 动态选 schema（当前仅 NecklineConfig；未来新增策略在 _select_schema
# 注册即可）。这是本任务唯一逻辑改动——保持颈线法异步路径能用（replay_worker neckline
# 分支 cfg_override 透传给策略，不直接调 merge_cfg，但 merge_cfg 作为 generic cfg 装配
# 工具保留，供未来需要默认值合并的路径复用）。


# 模块级 logger：装配异常走 debug（不污染 prod 日志，但可调试追溯）
logger = logging.getLogger("data.price_loader")


def _select_schema(strategy_name: str):
    """按 strategy_name 选对应的 pydantic config schema（延迟 import 打破循环）。

    Task 1.3：caisen 形态退役后仅颈线法活跃。未来新增策略在此注册即可（每策略一个
    pydantic BaseModel schema）。未知 strategy_name → 抛 ValueError（防静默用错 schema
    校验出"假合法"的 cfg_override）。
    """
    if strategy_name == "neckline":
        from strategies.neckline_schema import NecklineConfig
        return NecklineConfig
    raise ValueError(
        f"未知 strategy_name={strategy_name!r}，无对应 config schema"
        "（caisen 形态已退役，当前仅支持 'neckline'）")


def load_price_data(symbols: Optional[List[str]], date: str) -> Dict[str, pd.DataFrame]:
    """按标的池 + 日期从 data_lake 装配 price_data（生产走 DataLakeReader）。

    物理意图：
        生产：DataLakeReader.get_instance()（lifespan 已 load daily 湖进内存），
              按 symbols 逐标的 get_timeseries 取时序并装配。
        universe 语义：symbols 为 None 或 [] → 全市场枚举（reader.symbols）。
        单位统一：data_lake amount 为千元（tushare pro.daily 原生），装配时 ×1000 转元，
              与 risk.liquidity_min_amount=1e8(元) 口径一致（#3）。
        离线降级：reader 未 load / 湖空 / 全部 symbol 取空 → 返 {}，scan/replay
              按既有契约降级（空候选 / 零统计），不抛异常。

    参数：
        symbols: 标的池（ScanRequest.universe / ReplayRequest.universe）。
                 None 或 [] → 全市场。
        date:    截止交易日（scan 传 T 日取 [:T] 无前视；replay 传 req.end 取全历史段）。

    返回：
        {symbol: DataFrame}（OHLCV + amount 已转元）。离线/空时返回 {}。
    """
    from data.lake_reader import DataLakeReader
    from config import LAKE_CONFIG

    reader = DataLakeReader.get_instance()
    lake = LAKE_CONFIG.get("default_lake") or "daily"
    # ensure daily 湖已 load：server lifespan 启动时 load，但独立进程（celery worker /
    # 脚本）无 lifespan，此处自确保（守卫防重复 load；首次 load 408MB 进内存）。
    if not reader.loaded or lake not in reader.lakes():
        daily_path = LAKE_CONFIG["lakes"].get(lake)
        if daily_path and os.path.exists(daily_path):
            reader.load(daily_path, key=lake)
    if not reader.loaded:
        logger.debug("load_price_data 离线（daily 湖缺失/加载失败），返空 dict")
        return {}

    # universe 解析：None/空 → 全市场枚举
    if not symbols:
        symbols = reader.symbols(lake)
        if not symbols:
            logger.debug("load_price_data 湖无 symbol（lake=%s），返空 dict", lake)
            return {}

    price_data: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        # start 用足够早的固定日期（早于 daily 湖起点 2016），get_timeseries 的
        # .loc[start:end] 闭区间切片自然截到实际数据范围；end=date 截到 T 日（无前视）。
        ts = reader.get_timeseries(sym, start="2010-01-01", end=date, lake=lake)
        if ts is None or ts.empty:
            continue
        # 列对齐（screener 需 close/high/low/volume/amount）
        cols = [c for c in ("open", "high", "low", "close", "volume", "amount")
                if c in ts.columns]
        ts = ts[cols].copy()
        # #3 单位统一：amount 千元 → 元（流动性过滤口径统一；volume 手单位不影响策略，
        # 放量校验全用比例 right_vol_shrink/breakout_vol_multiplier，单位无关）。
        if "amount" in ts.columns:
            ts["amount"] = ts["amount"] * 1000.0
        price_data[sym] = ts
    return price_data


def merge_cfg(cfg_override: Dict[str, Any], strategy_name: str = "neckline"):
    """默认 config schema + cfg_override 增量合并（extra=forbid 动态子类全字段校验）。

    物理意图：用户传入 cfg_override（如 {"min_rr_ratio": 1.5}）增量覆盖默认配置，
    不修改全局默认 cfg 实例（每次重新 model_validate 构造新对象）。schema 按 strategy_name
    动态选择（Task 1.3：caisen 形态退役，当前仅颈线法 NecklineConfig）。

    防御性（Task 3 review I-1 校准，Task 1.3 schema 解耦）：
        Pydantic v2 有两个静默陷阱会掩盖非法 cfg_override：
          (1) `model_copy(update=...)` 是【不触发校验】的浅拷贝——未知字段名会被
              静默当作新属性附加，不抛 ValidationError；
          (2) `model_validate` 的默认 extra="ignore" 会静默丢弃未知字段名。
          二者都会让"cfg_override 字段名拼错"（如 {"min_rr_ration": 1.5}）被前端
          误当作"无候选"而非"参数错误"，掩盖配置 Bug。
          故此函数构建一个 extra="forbid" 的动态子类做全字段校验——未知字段 / 类型
          不匹配 / 约束违反（ge/le）统一抛 ValidationError。调用方不静默吞，让其上抛
          路由层转 422（参数错误）。

    参数：
        cfg_override:  参数覆盖 dict（键必须在该策略 config schema 的 model_fields 内）。
        strategy_name: 策略名（决定选哪个 pydantic schema 做校验，默认 "neckline"）。
    """
    # 延迟 import 打破 data.price_loader ↔ strategies 循环（见模块顶注释）。
    from pydantic import ConfigDict, create_model

    schema = _select_schema(strategy_name)
    base = schema()
    if not cfg_override:
        return base
    # extra="forbid" 动态子类：Pydantic v2 默认 extra="ignore" 静默丢弃未知字段，
    # 故临时开 forbid 才能让拼错字段名 → ValidationError 透传路由层转 422。
    # 用 create_model 生成子类保持 schema 全部字段/约束不变，仅叠加 forbid。
    _ForbidExtra = create_model(
        "_ForbidExtraConfig",
        __base__=schema,
        __config__=ConfigDict(extra="forbid"),
    )
    merged: Dict[str, Any] = {**base.model_dump(), **cfg_override}
    return _ForbidExtra.model_validate(merged)
