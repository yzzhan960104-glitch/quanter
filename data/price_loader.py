# -*- coding: utf-8 -*-
"""price_data 装配 + cfg 合并的模块级函数（Step4e 抽自 caisen/facade.py）。

物理意图（Step4e 反向债收口）：
    原 ``CaisenFacade._load_price_data`` / ``CaisenFacade._merge_cfg`` 是 facade 实例方法，
    但二者【不使用任何 self 状态】（_merge_cfg 完全无 self；_load_price_data 仅用模块级
    logger）。execution/replay_worker.py 的反向依赖 ``from server.services.caisen_service
    import _load_price_data, _merge_cfg``（模型层 execution → 服务层 server，Step2.2 过渡债）
    靠这两个 facade 实例方法的兼容转发维持。

    Step4e 把二者的【纯逻辑】抽到本模块（data/price_loader.py）成为模块级函数：
        - facade 调本模块（消除 facade 私有方法的双份真理）；
        - replay_worker 改 ``from data.price_loader import load_price_data as _load_price_data,
          merge_cfg as _merge_cfg``（模块级名字，保 replay_worker._load_price_data 测试
          monkeypatch 语义；同时去 caisen_service 反向依赖）；
        - caisen_service 删 _load_price_data/_merge_cfg 兼容转发块（Step2.2 过渡债消除）。

搬运纪律（strangler 红线·逻辑零改动）：
    本文件函数体【逐行原样搬】自 caisen/facade.py 的 _load_price_data / _merge_cfg，仅做：
        ① def f(self, args) → def f(args)（去 self）；
        ② facade 内 self._merge_cfg / self._load_price_data 调用点改调本模块函数；
        ③ logger 改 ``logging.getLogger("data.price_loader")``（模块级，与 facade logger 同级）。
    算法 / 参数 / 异常处理 / 降级逻辑 / 注释【一字不改】。

定位选择（为什么放 data/ 不放 caisen/）：
    load_price_data 的物理职责是「从 data_lake 装配时序」，依赖 data.lake_reader +
    config.LAKE_CONFIG，归属 data/ 层最自然（caisen 是消费方）。merge_cfg 是 cfg 装配
    工具，随 load_price_data 同搬（worker 二者一起 import）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import pandas as pd

# 注：StrategyConfig 的 import 故意延迟到 merge_cfg 函数内部（不放模块顶）。原因：
# data.price_loader 位于 data/ 包（与 caisen/ 平级），模块顶 ``from caisen.engines.config
# import StrategyConfig`` 会触发 caisen/__init__.py 全量初始化 → caisen.optimize →
# caisen.infra.replay_tasks_db → caisen.infra.storage → execution/__init__ →
# execution.replay_worker → ``from data.price_loader import load_price_data`` 形成循环
# （data.price_loader 半初始化时被反向 import）。延迟到函数体内打破循环——首次调
# merge_cfg 时 caisen 包已完全初始化，StrategyConfig 可安全取到。load_price_data 本身
# 不依赖 StrategyConfig，不受影响。


# 模块级 logger：装配异常走 debug（不污染 prod 日志，但可调试追溯）
logger = logging.getLogger("data.price_loader")


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


def merge_cfg(cfg_override: Dict[str, Any]) -> "StrategyConfig":
    """默认 StrategyConfig + cfg_override 增量合并（extra=forbid 动态子类全字段校验）。

    物理意图：用户传入 cfg_override（如 {"min_rr_ratio": 1.5}）增量覆盖默认配置，
    不修改全局默认 cfg 实例（每次重新 model_validate 构造新对象）。

    防御性（Task 3 review I-1 校准）：
        Pydantic v2 有两个静默陷阱会掩盖非法 cfg_override：
          (1) `model_copy(update=...)` 是【不触发校验】的浅拷贝——未知字段名会被
              静默当作新属性附加，不抛 ValidationError；
          (2) `model_validate` 的默认 extra="ignore" 会静默丢弃未知字段名。
          二者都会让"cfg_override 字段名拼错"（如 {"min_rr_ration": 1.5}）被前端
          误当作"无候选"而非"参数错误"，掩盖配置 Bug。
          故此函数构建一个 extra="forbid" 的动态子类（_ForbidExtraConfig）做全字段
          校验——未知字段 / 类型不匹配 / 约束违反（ge/le）统一抛 ValidationError。
          facade 层不静默吞，让其上抛路由层转 422（参数错误）。
    """
    # 延迟 import 打破 data.price_loader ↔ caisen 循环（见模块顶注释）。
    from caisen.engines.config import StrategyConfig
    from pydantic import ConfigDict, create_model

    base = StrategyConfig()
    if not cfg_override:
        return base
    # extra="forbid" 动态子类：Pydantic v2 默认 extra="ignore" 静默丢弃未知字段，
    # 故临时开 forbid 才能让拼错字段名 → ValidationError 透传路由层转 422。
    # 用 create_model 生成子类保持 StrategyConfig 全部字段/约束不变，仅叠加 forbid。
    _ForbidExtra = create_model(
        "_ForbidExtraConfig",
        __base__=StrategyConfig,
        __config__=ConfigDict(extra="forbid"),
    )
    merged: Dict[str, Any] = {**base.model_dump(), **cfg_override}
    return _ForbidExtra.model_validate(merged)
