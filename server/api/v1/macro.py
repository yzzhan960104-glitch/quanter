# -*- coding: utf-8 -*-
"""宏观/板块/因子只读端点（读内存湖，零写入）。

定位：宏观 CTA 前端驾驶舱（T17 /dashboard）的唯一后端供给。
四个 GET 端点全部【只读】内存湖（DataLakeReader）+ CreditRegime 单例，
绝不触发任何 parquet 写入或网络拉取——保护数据湖在请求路径上的不可变性。

端点清单：
    - GET /api/v1/macro/regime       当前宏观信贷状态 + 近 N 日历史迁移
    - GET /api/v1/macro/credit       社融/M1M2_gap/dr007 时序（信贷三因子曲线）
    - GET /api/v1/macro/sector/flow  板块资金流排名 + 活跃股池
    - GET /api/v1/macro/factors/{symbol}  单标的 ATR 波动率（微观定权）

离线降级红线（贯穿全部端点）：
    开发机/CI 无数据湖（parquet 缺失、lifespan 未 load 任何湖）时，端点必须返
    【空结构】而非抛 500，让前端能渲染空图表容错；任何 raise 都会导致前端
    整页白屏。故每条端点顶部均做「无湖/空 df → 返空」短路返回。

无前视红线：
    CreditRegime.compute/history 内部已用 .loc[:date] 严格时间门控，
    本层只透传结果，不参与时间切片；ATR 读取历史窗口亦只取过去值。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

import pandas as pd
from fastapi import APIRouter

from data.lake_reader import DataLakeReader
from core.macro_regime import CreditRegime

router = APIRouter(prefix="/macro", tags=["宏观/板块/因子"])


# --------------------------------------------------------------
# 辅助：日期工具（端点内联，避免污染 config 层）
# --------------------------------------------------------------

def _today() -> str:
    """今日日期（YYYY-MM-DD 字符串）。

    Why 字符串：DataLakeReader 内部 _norm_date 会按湖 date 层级原生 dtype
    归一化查询键，端点侧统一传字符串最稳妥（兼容 str/datetime 两类索引）。
    """
    return _dt.date.today().strftime("%Y-%m-%d")


def _shift(days: int) -> str:
    """今日往前推 days 日的日期字符串。

    用途：/macro/factors ATR 读取 [today-30, today] 窗口的分钟时序，
    30 日窗口保证 ATR 滚动均值有足够样本（14 bar 默认窗口 + 余量）。
    """
    return (_dt.date.today() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")


def _fmt_date(i: Any) -> str:
    """把索引值统一格式化为 YYYY-MM-DD 字符串（前端 v-chart 友好）。

    Why 不用裸 str(i)：
      - DatetimeIndex 的 str() 会带上 '00:00:00' 时分秒（如 '2024-01-01 00:00:00'），
        前端图表 X 轴不需要时分秒，且会污染日期聚合；
      - str 层级索引（macro 湖落盘为 str 时）原样返回即可。
    Timestamp 走 strftime('%Y-%m-%d') 截掉时分秒，其它类型回退到 str()。
    """
    if isinstance(i, (pd.Timestamp, _dt.datetime, _dt.date)):
        return i.strftime("%Y-%m-%d")
    return str(i)


def _series_to_json(s: pd.Series) -> list[dict[str, Any]]:
    """把 Pandas 单列序列转为前端 v-chart 友好的 [{date, value}] 列表。

    - dropna()：剔除停牌/未公布日的 NaN，避免前端绘出断点（如月频社融在
      非公布日为 NaN）。
    - tail(180)：取近 180 个观测点（≈ 半年工作日），前端图表面板足够看趋势，
      又不会因序列过长拖慢渲染。
    - _fmt_date(i)：日期统一 YYYY-MM-DD 字符串化，规避 Timestamp 带时分秒或
      序列化为 epoch ms 的歧义。
    - float(v)：强制 float，杜绝 numpy 标量序列化为不可控类型。
    """
    return [
        {"date": _fmt_date(i), "value": float(v)}
        for i, v in s.dropna().tail(180).items()
    ]


def _history_to_list(history: Any) -> list[dict[str, Any]]:
    """把 CreditRegime.history 的返回（Series 或 list）统一为 [{date, regime}]。

    Why 兼容两种返回形态：
      - 生产 CreditRegime.history 返 pd.Series（index=date, values=+1/0/-1）；
      - 测试替身（_FakeRegime）直接返 [{date, regime}] 列表。
    端点对两种形态统一归一化为列表，前端无需关心后端实现细节。
    """
    if isinstance(history, pd.Series):
        return [
            {"date": _fmt_date(d), "regime": int(v)}
            for d, v in history.items()
        ]
    # 已是 list[dict] 形态（测试替身）或其它可迭代对象 → 原样返回
    if isinstance(history, list):
        return history
    # 兜底：未知类型返空列表，绝不抛异常破坏端点契约
    return []


# --------------------------------------------------------------
# 端点 1：/macro/regime —— 当前宏观信贷状态 + 历史
# --------------------------------------------------------------

@router.get("/regime", summary="当前宏观信贷状态 + 近 N 日历史")
async def regime() -> dict[str, Any]:
    """返回当前 CreditRegime 状态与近 60 日历史迁移。

    响应结构：
        {
          "regime": +1 | 0 | -1,              # 当日扩张/中性/收缩
          "history": [{"date","regime"}, ...]  # 近 60 日逐日状态序列
        }

    Why get_default().compute(today)：单例复用，避免每请求重载 macro 湖；
    compute 内部 _series(today) 已 .loc[:today] 严格时间门控，无前视。
    """
    r = CreditRegime.get_default().compute(_today())
    # history 默认近 60 日（前端驾驶舱红黄绿迁移带，60 日 ≈ 1 季度工作日）
    raw_history = CreditRegime.get_default().history(60)
    return {
        "regime": int(r),
        "history": _history_to_list(raw_history),
    }


# --------------------------------------------------------------
# 端点 2：/macro/credit —— 信贷三因子时序
# --------------------------------------------------------------

@router.get("/credit", summary="社融/M1M2_gap/DR007 时序")
async def credit() -> dict[str, Any]:
    """返回 macro 湖各列的时序（社融/M1M2_gap/dr007 信贷三因子）。

    响应结构：
        {"series": {列名: [{date, value}, ...], ...}}

    离线降级：macro 湖未载入（离线/CI 无 parquet）→ 返 {series: {}} 不抛，
    前端渲染空图表容错。

    Why 直读 _lakes["macro"] 而非 get_timeseries：macro 湖是 DatetimeIndex
    （无 symbol 层，全市场级宏观指标），DataLakeReader.get_timeseries 内部
    df.xs(symbol, level="symbol") 对无 symbol 层的湖会抛 KeyError，故必须直读。
    """
    reader = DataLakeReader.get_instance()
    df = reader._lakes.get("macro")
    # 离线降级短路：无 macro 湖或空 df → 返空结构（不抛，前端容错）
    if df is None or df.empty:
        return {"series": {}}
    # 逐列转 [{date, value}]：宏观指标通常是月频，dropna 剔除非公布日空洞
    return {"series": {c: _series_to_json(df[c]) for c in df.columns}}


# --------------------------------------------------------------
# 端点 3：/macro/sector/flow —— 板块资金流排名 + 活跃股池
# --------------------------------------------------------------

@router.get("/sector/flow", summary="板块资金流排名 + 活跃股池")
async def sector_flow() -> dict[str, Any]:
    """返回板块资金流 Top N 排名与活跃股池。

    响应结构：
        {
          "sectors": [板块记录 dict, ...],  # head(20) 板块资金流排名
          "pool":     [活跃股代码, ...]       # 活跃股池
        }

    离线降级：sector 湖未载入 → 返 {sectors: [], pool: []} 不抛。

    Why head(20)：前端面板仅展示 Top 20 板块，避免一次性渲染过多行拖慢交互。
    Why pool 暂返 []：活跃股池目前由 sync_sector_daily 单独落盘到独立结构，
    本期端点先留字段占位（前端契约已对齐），下期接入活跃股池湖后填充。
    """
    import os as _os
    import pandas as _pd
    from config import LAKE_CONFIG
    # sector 资金流是【快照排名表】（RangeIndex，非时序），DataLakeReader 只载时序湖会跳过它，
    # 故此处直读 parquet；活跃股池从 daily 湖的唯一标的取（select_active_pool 选出的 50 只）。
    sectors: list = []
    pool: list = []
    _sp = LAKE_CONFIG["lakes"]["sector"]
    if _os.path.exists(_sp):
        _sdf = _pd.read_parquet(_sp)
        if _sdf is not None and not _sdf.empty:
            sectors = _sdf.head(20).to_dict("records")
    _dp = LAKE_CONFIG["lakes"]["daily"]
    if _os.path.exists(_dp):
        _ddf = _pd.read_parquet(_dp)
        if hasattr(_ddf.index, "get_level_values"):
            _syms = list(_ddf.index.get_level_values("symbol").unique())[:50]
            pool = [{"symbol": str(s)} for s in _syms]
    return {"sectors": sectors, "pool": pool}


# --------------------------------------------------------------
# 端点 4：/macro/factors/{symbol} —— 单标的 ATR 波动率
# --------------------------------------------------------------

@router.get("/factors/{symbol}", summary="单标的 ATR 波动率")
async def factors(symbol: str) -> dict[str, Any]:
    """返回单标的近 30 日窗口的 ATR 波动率（微观 Risk Parity 定权用）。

    响应结构：
        {"atr": float | None}   # ATR 值，时序空时返 None

    离线降级：minute 湖未载入或 symbol 无时序 → get_timeseries 返空 df → 返
    {atr: None} 不抛（前端显示「无数据」而非崩溃）。

    Why get_timeseries(symbol, ...) 而非直读 _lakes：minute 湖是 MultiIndex
    (date, symbol)，标准 xs 查询路径正确；与 macro/sector 端点不同。
    Why 30 日窗口：ATR 默认 14 bar 滚动窗口，30 日保证有足够样本算出末值，
    又不会因窗口过长引入过时波动率。
    """
    # 延迟 import：仅此端点用 ATR，避免顶层 import 污染其它三端点的导入图。
    # ATR 已从 factors/micro_momentum 迁到 core/indicator（Phase 1·Task 3 因子体系剥离）。
    from core.indicator import atr

    ts = DataLakeReader.get_instance().get_timeseries(
        symbol, _shift(30), _today(), lake="minute"
    )
    # 离线降级：时序空（无湖/symbol 不存在/窗口无数据）→ 返 None
    if ts.empty:
        return {"atr": None}
    # ATR 末值：micro_momentum.atr 返 Series，取 .iloc[-1] 即最新一日 ATR
    v = atr(ts).iloc[-1]
    # ★ NaN/不足窗口守卫（双保险）：
    #   1) 窗口不足守卫：atr() 默认 14 bar 滚动窗口，当 minute 湖 bar 数 < 14 时
    #      rolling 末值本应为 NaN——但 micro_momentum.atr 内部 .where(a>1e-9, 1e-9)
    #      会把 NaN 一并替换成 1e-9（防除零 ε），即「窗口不足」被静默伪装成 1e-9 的
    #      伪 ATR。这比裸 NaN 更危险：前端会信以为真地画出错误的微观波动率定权。
    #      故必须在端点侧显式按序列长度判窗口，bar 数不足 → 返 None（语义=数据不足）。
    #   2) NaN 直通守卫：即便未来 atr() 实现变更不再以 ε 兜底，pd.isna(v) 仍能把
    #      裸 NaN 降级为 None——float(NaN) 会让 FastAPI 默认 json 编码器发出字面
    #      "NaN" token（非法 JSON，JS JSON.parse/前端 axios 抛 SyntaxError 致整页
    #      白屏），违背本文件「绝不致前端白屏」降级红线。两层守卫缺一不可。
    if len(ts) < 14 or pd.isna(v):
        return {"atr": None}
    return {"atr": float(v)}
