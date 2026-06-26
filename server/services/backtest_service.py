# -*- coding: utf-8 -*-
"""
单资产回测服务层

职责：
1. 接收路由层传来的已校验参数
2. 实例化数据获取器、因子计算、回测引擎
3. 执行回测流程（与 example.py 一致，但参数化）
4. 将引擎结果（含 DataFrame）序列化为 JSON 安全的响应模型

设计原则：
- 此层是引擎与 API 的桥梁，负责 DataFrame → JSON 的转化
- 序列化时必须精简：仅传输绘图必需的字段，丢弃冗余 K 线数据
- 异常不在此层捕获，由路由层统一处理并转为 HTTPException
"""
from datetime import datetime
from typing import Dict, Any

import numpy as np
import pandas as pd

from data.fetcher import MockDataFetcher
from data.cleaner import DataCleaner
from factors.technical import moving_average_cross, volume_price_trend
from factors.macro import macro_anchor_signal
from factors.fusion import signal_fusion
from backtest.engine import BacktestEngine
from backtest.cost_model import CostModel

from server.schemas.backtest import (
    BacktestRequest,
    BacktestResponse,
    MetricsResponse,
    NavPoint,
    DrawdownPoint,
    TradeRecord,
)
from server.core.config import DATA_DEFAULTS


def run_single_backtest(req: BacktestRequest) -> BacktestResponse:
    """
    执行单资产回测

    完整流程（与 example.py 对齐）：
    1. MockDataFetcher 获取 OHLCV + 宏观数据
    2. DataCleaner 清洗数据
    3. 计算技术信号（双均线 + VPT → 融合）
    4. 计算宏观信号（M2 锚点）
    5. 信号融合（tech_weights 加权）
    6. BacktestEngine.run() 执行回测
    7. 序列化结果为 BacktestResponse

    参数：
        req: 已校验的回测请求参数

    返回：
        BacktestResponse（JSON 安全）

    异常：
        任何引擎/数据异常直接向上抛出，由路由层捕获
    """
    # ============ 步骤 1：获取数据 ============
    fetcher = MockDataFetcher(seed=DATA_DEFAULTS["mock_seed"])

    start_dt = datetime.combine(req.start_date, datetime.min.time())
    end_dt = datetime.combine(req.end_date, datetime.min.time())

    # 获取 OHLCV 数据
    df = fetcher.fetch_ohlcv(req.symbol, start_dt, end_dt, freq=req.signal_freq)

    # 获取宏观数据（M2 增速）
    macro_df = fetcher.fetch_macro("m2", start_dt, end_dt)

    # ============ 步骤 2：清洗数据 ============
    cleaner = DataCleaner()
    df_clean = cleaner.clean_ohlcv(df, max_fill=5)

    # 对齐多频率数据
    try:
        df_aligned = cleaner.align_frequencies(df_clean, macro_df)
    except ValueError:
        # 宏观数据对齐失败时退化为纯技术信号
        df_aligned = df_clean.copy()
        df_aligned["m2"] = 200.0  # 虚拟宏观列

    # ============ 步骤 3：计算技术信号 ============
    # 双均线交叉信号
    ma_signal = moving_average_cross(df_aligned, short_window=5, long_window=20)

    # 量价趋势信号
    vpt_signal = volume_price_trend(df_aligned, window=20)

    # 技术信号融合（简单平均）
    tech_signal = (ma_signal + vpt_signal) / 2

    # ============ 步骤 4：计算宏观信号 ============
    try:
        macro_signal = macro_anchor_signal(macro_df, indicator="m2", threshold=0.02, window=3)
    except Exception:
        # 宏观信号计算失败时使用中等多头信号
        macro_signal = pd.Series(0.5, index=tech_signal.index)

    # ============ 步骤 5：信号融合 ============
    # 对齐信号索引（技术信号日频 vs 宏观信号月频，取交集）
    aligned_index = tech_signal.index.intersection(macro_signal.index)
    tech_aligned = tech_signal.loc[aligned_index]
    macro_aligned = macro_signal.loc[aligned_index]

    fused_signal = signal_fusion(
        tech_aligned,
        macro_aligned,
        weights=req.tech_weights
    )

    # ============ 步骤 6：执行回测 ============
    # 构建成本模型
    cost_params = req.cost_model
    if cost_params is None:
        cost_model = CostModel()
    else:
        cost_model = CostModel(
            commission_rate=cost_params.commission_rate,
            stamp_duty=cost_params.stamp_duty,
            min_commission=cost_params.min_commission,
            slippage_model=cost_params.slippage_model,
            slippage_rate=cost_params.slippage_rate,
            liquidity_threshold=cost_params.liquidity_threshold,
        )

    # 初始化引擎
    engine = BacktestEngine(
        initial_capital=req.initial_capital,
        cost_model=cost_model,
        signal_freq=req.signal_freq,
    )

    # 执行回测（使用对齐后的数据）
    df_for_backtest = df_aligned.loc[fused_signal.index].copy()
    result = engine.run(df_for_backtest, fused_signal, symbol=req.symbol)

    # ============ 步骤 7：序列化结果 ============
    return _serialize_backtest_result(result)


def _serialize_backtest_result(result: Dict[str, Any]) -> BacktestResponse:
    """
    将引擎结果序列化为 BacktestResponse

    核心优化：
    - 从 daily_records (DataFrame) 中仅提取绘图必需的 4 个字段
    - 丢弃 cash / position / position_value / price / signal 等冗余列
    - 单独计算 drawdown_series（前端画回撤填充区需要）
    - 从 trades (DataFrame) 中仅提取 date/direction/shares/price/cost

    参数：
        result: 引擎返回的原始结果字典

    返回：
        BacktestResponse（JSON 安全）
    """
    daily_df: pd.DataFrame = result["daily_records"]
    trades_df: pd.DataFrame = result["trades"]

    # ============ 提取净值时序（精简 4 字段） ============
    nav_series: list[NavPoint] = []
    for idx, row in daily_df.iterrows():
        nav_series.append(NavPoint(
            date=idx.strftime("%Y-%m-%d") if isinstance(idx, pd.Timestamp) else str(idx),
            nav=_safe_float(row.get("nav", 0.0)),
            return_=_safe_float(row.get("return", 0.0)),
            cumulative_return=_safe_float(row.get("cumulative_return", 0.0)),
        ))

    # ============ 计算回撤时序 ============
    # 引擎已计算 max_drawdown，但前端需要逐日回撤序列画填充区
    daily_returns = daily_df["nav"].pct_change().fillna(0.0)
    cumulative = (1 + daily_returns).cumprod()
    rolling_max = cumulative.expanding().max()
    drawdown = (cumulative - rolling_max) / rolling_max

    drawdown_series: list[DrawdownPoint] = []
    for idx, dd_val in drawdown.items():
        drawdown_series.append(DrawdownPoint(
            date=idx.strftime("%Y-%m-%d") if isinstance(idx, pd.Timestamp) else str(idx),
            drawdown=_safe_float(dd_val),
        ))

    # ============ 提取交易记录（精简 5 字段） ============
    trades: list[TradeRecord] = []
    if len(trades_df) > 0:
        for _, row in trades_df.iterrows():
            trades.append(TradeRecord(
                date=row["date"].strftime("%Y-%m-%d")
                if isinstance(row["date"], pd.Timestamp) else str(row["date"]),
                direction=str(row["direction"]),
                shares=int(row["shares"]),
                price=_safe_float(row["price"]),
                cost=_safe_float(row["cost"]),
            ))

    # ============ 构建响应 ============
    return BacktestResponse(
        metrics=MetricsResponse(
            initial_capital=_safe_float(result["initial_capital"]),
            final_nav=_safe_float(result["final_nav"]),
            total_return=_safe_float(result["total_return"]),
            annual_return=_safe_float(result["annual_return"]),
            annual_volatility=_safe_float(result["annual_volatility"]),
            max_drawdown=_safe_float(result["max_drawdown"]),
            sharpe_ratio=_safe_float(result["sharpe_ratio"]),
            calmar_ratio=_safe_float(result["calmar_ratio"]),
            win_rate=_safe_float(result["win_rate"]),
            profit_loss_ratio=_safe_float(result["profit_loss_ratio"]),
            n_trades=int(result["n_trades"]),
            n_failed_trades=int(result["n_failed_trades"]),
        ),
        nav_series=nav_series,
        drawdown_series=drawdown_series,
        trades=trades,
    )


def _safe_float(val: Any) -> float:
    """
    安全转换为 Python float

    防范 numpy.float64 / numpy.int64 序列化异常：
    - FastAPI 的 jsonable_encoder 无法处理 numpy 类型
    - NaN / Inf 转为 0.0（防范 JSON 规范不允许的 NaN/Infinity）

    参数：
        val: 待转换的数值

    返回：
        Python 原生 float
    """
    try:
        f = float(val)
    except (TypeError, ValueError):
        return 0.0

    # 防范 NaN / Inf（JSON 规范不允许）
    if not np.isfinite(f):
        return 0.0

    return f
