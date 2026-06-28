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
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from data.fetcher import MockDataFetcher
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
from server.core.config import DATA_DEFAULTS


def run_portfolio_backtest(req: PortfolioRequest) -> PortfolioResponse:
    """
    执行组合回测（HMM 逻辑已迁入 HMMMacroStrategy，标量参数经 strategy_params 注入）

    ── 策略驱动架构（Task 8）──
    原本散落在 service 的 HMM 训练/状态映射/迟滞滤波逻辑，已统一封装进
    HMMMacroStrategy.fit/generate_target_weights。service 层只负责：
    1. MockDataFetcher 取各标的 OHLCV + 宏观 M2
    2. 用 HmmMacroParams 显式校验注入 HMM 标量参数（covariance/n_iter/release_lag/max_fill_days）
    3. 实例化 HMMMacroStrategy（结构配置：universe/n_hmm_states/state_weights/buffer_threshold）
    4. fit(price_data, macro) → generate_target_weights
    5. BacktestEngine.run_portfolio 执行组合回测
    6. 序列化结果

    参数说明（反黑盒）：
    - random_state=42 在 HMMMacroStrategy 内部硬编码（保证 HMM 训练可复现）
    - 结构配置（状态数/权重/迟滞阈值）来自 PortfolioRequest 既有字段
    - 标量超参来自 strategy_params，经 HmmMacroParams 校验注入
    """
    from strategies.hmm_macro_strategy import HMMMacroStrategy, HmmMacroParams
    from strategies.base import StrategyContext

    # ============ 步骤 1：取数 ============
    fetcher = MockDataFetcher(seed=DATA_DEFAULTS["mock_seed"])
    start_dt = datetime.combine(req.start_date, datetime.min.time())
    end_dt = datetime.combine(req.end_date, datetime.min.time())

    price_data: Dict[str, pd.DataFrame] = {
        s: fetcher.fetch_ohlcv(s, start_dt, end_dt, freq="1d")
        for s in req.symbols
    }
    macro_df = fetcher.fetch_macro("m2", start_dt, end_dt)

    # ============ 步骤 2：校验注入 HMM 标量参数 ============
    # 显式 params_model(**dict) 构造，禁 **kwargs 黑盒；非法参数在此抛 ValidationError
    hmm_params = HmmMacroParams(**(req.strategy_params or {}))

    strategy = HMMMacroStrategy(
        universe=req.symbols,
        params=hmm_params,
        n_hmm_states=req.n_hmm_states,
        state_weights=req.state_weights,
        buffer_threshold=req.buffer_threshold,
    )

    # ============ 步骤 3：训练 + 产出信号 ============
    strategy.fit(price_data, macro_data=macro_df)
    ctx = StrategyContext(
        timestamp=start_dt,
        current_weights={s: 0.0 for s in req.symbols},
        cash=req.initial_capital,
        aum=req.initial_capital,
    )
    signals = strategy.generate_target_weights(price_data, ctx)

    # ============ 步骤 4：执行回测 ============
    engine = BacktestEngine(initial_capital=req.initial_capital)
    result = engine.run_portfolio(price_data=price_data, signals=signals)

    # ============ 步骤 5：序列化结果 ============
    return _serialize_portfolio_result(result)


def _serialize_portfolio_result(result: Dict[str, Any]) -> PortfolioResponse:
    """
    将组合回测引擎结果序列化为 PortfolioResponse（纯向量化，无 iterrows）

    性能优化对齐 backtest_service._serialize_backtest_result：
    - nav / drawdown：纯向量化 + 列式提取（orient='list'）
    - NaN / Inf → None：JSON 规范不允许，用 where(np.isfinite) 向量化替换
    - weight：含字典类型列（position_values），用 .tolist() 取列后 zip 构建，
      规避 iterrows（iterrows 会把每行打包成 Series，dtype 推断错乱且慢 N 倍）
    - trades：向量化日期格式化 + 列式提取

    与单资产序列化的差异：
    - 额外提取 weight_series（每日各资产权重快照）

    参数：
        result: 引擎返回的原始结果字典

    返回：
        PortfolioResponse（JSON 安全）
    """
    daily_df: pd.DataFrame = result["daily_records"]
    trades_df: pd.DataFrame = result["trades"]

    # ── 日期序列（向量化 strftime，规避逐行 isinstance 判断）──
    if isinstance(daily_df.index, pd.DatetimeIndex):
        dates = daily_df.index.strftime("%Y-%m-%d").tolist()
    else:
        dates = [str(idx) for idx in daily_df.index]

    # ============ 净值时序（向量化 + 列式）============
    nav_cols = ["nav", "return", "cumulative_return"]
    # reindex 防御列缺失：兼容不同引擎版本返回的 daily_records 结构
    nav_data = daily_df.reindex(columns=nav_cols)

    # ── NaN / Inf → None（JSON 安全，向量化替换规避逐行判断）──
    nav_data = nav_data.where(np.isfinite(nav_data), None)
    nav_dict = nav_data.to_dict(orient="list")

    # 列式 → NavPoint（zip 构建，O(n) 且无 Series 包装开销）
    nav_series: List[NavPoint] = [
        NavPoint(
            date=d,
            nav=n,
            return_=r,
            cumulative_return=c,
        )
        for d, n, r, c in zip(
            dates,
            nav_dict["nav"],
            nav_dict["return"],
            nav_dict["cumulative_return"],
        )
    ]

    # ============ 回撤时序（纯向量化，无 iterrows）============
    # pct_change 首行为 NaN → fillna(0.0)，规避后续 cumprod / 除法产生异常值
    daily_returns = daily_df["nav"].pct_change().fillna(0.0)
    if len(daily_returns) > 0:
        daily_returns.iloc[0] = 0.0  # 首日无前值，收益率为 0

    cumulative = (1 + daily_returns).cumprod()
    rolling_max = cumulative.expanding().max()
    drawdown = (cumulative - rolling_max) / rolling_max

    # 向量化 NaN / Inf → None
    drawdown_safe = drawdown.where(np.isfinite(drawdown), None)

    drawdown_series: List[DrawdownPoint] = [
        DrawdownPoint(date=d, drawdown=dd)
        for d, dd in zip(dates, drawdown_safe.tolist())
    ]

    # ============ 权重时序（组合模式特有）============
    # daily_records 的 position_values 列为字典类型 {symbol: 市值}，
    # .tolist() 一次性取出为 list[dict]，规避 iterrows 的 Series 包装开销。
    position_values_list = daily_df.get("position_values", pd.Series(dtype=object)).tolist()
    positions_list = daily_df.get("positions", pd.Series(dtype=object)).tolist()
    nav_list = daily_df["nav"].tolist()

    weight_series: List[WeightPoint] = []
    for d, pv, pos, nav in zip(dates, position_values_list, positions_list, nav_list):
        weights: Dict[str, float] = {}
        # 仅当存在持仓市值字典且净值正常时计算权重；否则该日全空仓。
        # nav 可能为 NaN/0 → 显式归零，避免前端权重堆叠图出现 NaN 断层。
        if isinstance(pv, dict) and nav and nav > 0:
            for symbol, value in pv.items():
                weights[symbol] = _safe_float(value / nav)
        elif isinstance(pos, dict):
            # 退化路径：无市值但有持仓数量 → 权重置零，保持权重堆叠图连续
            weights = {symbol: 0.0 for symbol in pos.keys()}

        weight_series.append(WeightPoint(date=d, weights=weights))

    # ============ 交易记录（向量化提取，无 iterrows）============
    trades: List[TradeRecord] = []
    if len(trades_df) > 0:
        # 仅保留绘图必需列
        trade_cols = ["date", "direction", "shares", "price", "cost"]
        trades_subset = trades_df.reindex(columns=trade_cols).copy()

        # 向量化 NaN / Inf → None（price / cost 可能为空成交）
        for col in ["price", "cost"]:
            trades_subset[col] = trades_subset[col].where(
                np.isfinite(trades_subset[col]), None
            )

        # 向量化日期格式化（替代逐行 strftime）
        trades_subset["date"] = pd.to_datetime(trades_subset["date"]).dt.strftime("%Y-%m-%d")

        # 列式提取，zip 构建
        trade_dates = trades_subset["date"].tolist()
        trade_dirs = trades_subset["direction"].astype(str).tolist()
        trade_shares = trades_subset["shares"].astype(int).tolist()
        trade_prices = trades_subset["price"].tolist()
        trade_costs = trades_subset["cost"].tolist()

        trades = [
            TradeRecord(
                date=d,
                direction=direc,
                shares=shares,
                price=price,
                cost=cost,
            )
            for d, direc, shares, price, cost in zip(
                trade_dates, trade_dirs, trade_shares, trade_prices, trade_costs
            )
        ]

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
