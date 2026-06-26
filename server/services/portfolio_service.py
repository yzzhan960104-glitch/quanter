# -*- coding: utf-8 -*-
"""
组合回测服务层

职责：
1. 接收路由层传来的已校验组合参数
2. 实例化 MockDataFetcher、MacroRegimeHMM、HMMStateMapper、BacktestEngine
3. 执行完整组合回测流程：数据获取 → HMM 训练 → 状态映射 → 组合调仓
4. 序列化结果为 PortfolioResponse（含权重时序）

设计原则：
- HMM 训练使用日频 + 月频对齐数据（复用 MacroRegimeHMM.align_macro_data）
- 迟滞滤波参数由前端传入（buffer_threshold）
- 权重时序从 daily_records 的 positions 字段提取
- 与 backtest_service.py 共享 _safe_float 工具函数
"""
from datetime import datetime
from typing import Dict, Any, List

import numpy as np
import pandas as pd

from data.fetcher import MockDataFetcher
from factors.fusion import HMMStateMapper, AssetWeightConfig, SignalDirection
from factors.hmm_macro import MacroRegimeHMM
from backtest.engine import BacktestEngine

from server.schemas.portfolio import (
    PortfolioRequest,
    PortfolioResponse,
    MetricsResponse,
    NavPoint,
    DrawdownPoint,
    WeightPoint,
    TradeRecord,
)
from server.core.config import DATA_DEFAULTS, PORTFOLIO_DEFAULTS


def run_portfolio_backtest(req: PortfolioRequest) -> PortfolioResponse:
    """
    执行组合回测

    完整流程：
    1. MockDataFetcher 获取每个标的的 OHLCV + 宏观数据
    2. 清洗并合并数据
    3. 训练 HMM 模型（识别宏观状态）
    4. HMM 预测状态概率矩阵
    5. HMMStateMapper 映射为目标权重信号（含迟滞滤波）
    6. BacktestEngine.run_portfolio() 执行组合回测
    7. 序列化结果

    参数：
        req: 已校验的组合回测请求

    返回：
        PortfolioResponse（JSON 安全）
    """
    # ============ 步骤 1：获取各标的数据 ============
    fetcher = MockDataFetcher(seed=DATA_DEFAULTS["mock_seed"])

    start_dt = datetime.combine(req.start_date, datetime.min.time())
    end_dt = datetime.combine(req.end_date, datetime.min.time())

    price_data: Dict[str, pd.DataFrame] = {}
    for symbol in req.symbols:
        df = fetcher.fetch_ohlcv(symbol, start_dt, end_dt, freq="1d")
        price_data[symbol] = df

    # 获取宏观数据（用于 HMM 训练）
    macro_df = fetcher.fetch_macro("m2", start_dt, end_dt)

    # ============ 步骤 2：构建 HMM 训练数据 ============
    # 使用第一个标的的日频数据作为时间轴基准，拼接宏观指标
    base_symbol = req.symbols[0]
    daily_df = price_data[base_symbol][["close"]].copy()
    daily_df = daily_df.rename(columns={"close": f"{base_symbol}_close"})

    # 添加其他标的收盘价
    for symbol in req.symbols[1:]:
        if symbol in price_data:
            daily_df[f"{symbol}_close"] = price_data[symbol]["close"]

    # 对齐宏观数据到日频（严格防未来函数）
    hmm_model = MacroRegimeHMM(
        n_components=req.n_hmm_states,
        covariance_type=PORTFOLIO_DEFAULTS["hmm_covariance_type"],
        n_iter=PORTFOLIO_DEFAULTS["hmm_n_iter"],
        random_state=PORTFOLIO_DEFAULTS["hmm_random_state"],
    )

    aligned_df = hmm_model.align_macro_data(
        daily_df.dropna(),
        macro_df,
        release_lag=5,    # 模拟 5 天发布滞后
        max_fill_days=90,
    )

    # ============ 步骤 3：训练 HMM 模型 ============
    # 特征列：所有标的收盘价 + 宏观指标
    feature_columns = [col for col in aligned_df.columns
                       if not col.endswith("_freshness")]

    hmm_model.fit(aligned_df, feature_columns=feature_columns, drop_na=True)

    # ============ 步骤 4：HMM 预测状态概率矩阵 ============
    prob_matrix, entropy = hmm_model.predict(aligned_df, drop_na=False)

    # ============ 步骤 5：HMMStateMapper 映射目标权重信号 ============
    # 构建资产配置列表
    assets = [AssetWeightConfig(symbol=symbol, base_name=symbol) for symbol in req.symbols]

    mapper = HMMStateMapper(
        states=req.n_hmm_states,
        assets=assets,
        state_weights=req.state_weights,
        buffer_threshold=req.buffer_threshold,
    )

    # 批量映射：HMM 概率矩阵 → TargetWeightSignal 列表
    signals = mapper.map_states_to_weights(prob_matrix)

    # 重置 mapper 权重（确保下次调用从空仓开始）
    mapper.reset_weights()

    # ============ 步骤 6：执行组合回测 ============
    engine = BacktestEngine(
        initial_capital=req.initial_capital,
    )

    result = engine.run_portfolio(
        price_data=price_data,
        signals=signals,
    )

    # ============ 步骤 7：序列化结果 ============
    return _serialize_portfolio_result(result)


def _serialize_portfolio_result(result: Dict[str, Any]) -> PortfolioResponse:
    """
    将组合回测引擎结果序列化为 PortfolioResponse

    与单资产序列化的差异：
    - 额外提取 weight_series（每日各资产权重快照）
    - trades 中的 symbol 字段保留（组合模式多标的，symbol 非冗余）

    参数：
        result: 引擎返回的原始结果字典

    返回：
        PortfolioResponse（JSON 安全）
    """
    daily_df: pd.DataFrame = result["daily_records"]
    trades_df: pd.DataFrame = result["trades"]

    # ============ 提取净值时序 ============
    nav_series: List[NavPoint] = []
    for idx, row in daily_df.iterrows():
        nav_series.append(NavPoint(
            date=idx.strftime("%Y-%m-%d") if isinstance(idx, pd.Timestamp) else str(idx),
            nav=_safe_float(row.get("nav", 0.0)),
            return_=_safe_float(row.get("return", 0.0)),
            cumulative_return=_safe_float(row.get("cumulative_return", 0.0)),
        ))

    # ============ 计算回撤时序 ============
    daily_returns = daily_df["nav"].pct_change().fillna(0.0)
    # 首日收益率为 0
    if len(daily_returns) > 0:
        daily_returns.iloc[0] = 0.0

    cumulative = (1 + daily_returns).cumprod()
    rolling_max = cumulative.expanding().max()
    drawdown = (cumulative - rolling_max) / rolling_max

    drawdown_series: List[DrawdownPoint] = []
    for idx, dd_val in drawdown.items():
        drawdown_series.append(DrawdownPoint(
            date=idx.strftime("%Y-%m-%d") if isinstance(idx, pd.Timestamp) else str(idx),
            drawdown=_safe_float(dd_val),
        ))

    # ============ 提取权重时序（组合模式特有） ============
    # daily_records 中的 positions 字段格式：{symbol: shares}
    # position_values 字段格式：{symbol: value}
    weight_series: List[WeightPoint] = []
    for idx, row in daily_df.iterrows():
        position_values = row.get("position_values", {})
        nav = row.get("nav", 0.0)

        # 计算各资产权重 = 持仓市值 / 总净值
        weights: Dict[str, float] = {}
        if isinstance(position_values, dict) and nav > 0:
            for symbol, value in position_values.items():
                weights[symbol] = _safe_float(value / nav)
        else:
            # 无持仓或净值异常，权重全零
            for symbol in (row.get("positions", {}).keys() if isinstance(row.get("positions"), dict) else []):
                weights[symbol] = 0.0

        weight_series.append(WeightPoint(
            date=idx.strftime("%Y-%m-%d") if isinstance(idx, pd.Timestamp) else str(idx),
            weights=weights,
        ))

    # ============ 提取交易记录（组合模式保留 symbol） ============
    trades: List[TradeRecord] = []
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
    return PortfolioResponse(
        metrics=MetricsResponse(
            initial_capital=_safe_float(result["initial_capital"]),
            final_nav=_safe_float(result["final_nav"]),
            total_return=_safe_float(result["total_return"]),
            annual_return=_safe_float(result["annual_return"]),
            annual_volatility=_safe_float(result["annual_volatility"]),
            max_drawdown=_safe_float(result["max_drawdown"]),
            sharpe_ratio=_safe_float(result["sharpe_ratio"]),
            calmar_ratio=_safe_float(result["calmar_ratio"]),
            win_rate=_safe_float(result.get("win_rate", 0.0)),
            profit_loss_ratio=_safe_float(result.get("profit_loss_ratio", 0.0)),
            n_trades=int(result.get("n_trades", 0)),
            n_failed_trades=int(result.get("n_failed_trades", 0)),
        ),
        nav_series=nav_series,
        drawdown_series=drawdown_series,
        weight_series=weight_series,
        trades=trades,
    )


def _safe_float(val: Any) -> float:
    """
    安全转换为 Python float

    防范 numpy.float64 / numpy.int64 序列化异常，
    以及 NaN / Inf（JSON 规范不允许）

    参数：
        val: 待转换的数值

    返回：
        Python 原生 float
    """
    try:
        f = float(val)
    except (TypeError, ValueError):
        return 0.0

    if not np.isfinite(f):
        return 0.0

    return f
