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

性能红线：
- run_single_backtest() 是 CPU 密集型同步函数
- 绝对禁止在 async def 路由中直接调用，必须通过 run_in_threadpool 卸载到线程池
- 每次请求必须实例化全新的 BacktestEngine，绝不允许跨请求复用引擎实例
"""
from datetime import datetime
from typing import Any, Dict

import numpy as np
import pandas as pd

from data.fetcher import MockDataFetcher
from data.cleaner import DataCleaner
from backtest.engine import BacktestEngine
from backtest.cost_model import CostModel

from server.schemas.backtest import (
    BacktestRequest,
    BacktestResponse,
    MetricsResponse,
    NavPoint,
    DrawdownPoint,
    TradeRecord,
    OhlcvPoint,
    PositionRow,
)
from server.core.config import DATA_DEFAULTS


def run_single_backtest(req: BacktestRequest) -> BacktestResponse:
    """
    执行单资产回测（同步 CPU 密集函数）

    ── 事件循环阻塞警告 ──
    此函数包含 CPU 密集的回测引擎计算（逐日遍历 + 矩阵运算），
    直接在 async def 路由中调用会阻塞 FastAPI 事件循环，
    导致所有并发请求排队等待。必须在路由层通过 run_in_threadpool 调用。

    ── 全局状态污染警告 ──
    BacktestEngine 与策略在每次请求中全新创建，绝不允许跨请求复用。
    原因：引擎内部持有 cash/position/nav 等可变状态，策略持有 _macro_df
    等训练后状态，复用会导致请求 A 的状态泄漏到请求 B，产生不可复现结果。

    ── 策略驱动架构（Task 8）──
    统一走策略 + run_portfolio，不再在 service 层硬编码 MA/VPT/融合逻辑：
    1. MockDataFetcher 获取 OHLCV + 宏观数据
    2. DataCleaner 清洗 OHLCV
    3. StrategyLoader 按 req.strategy_name 取策略类（缺省 tech_macro_fusion）
    4. 用策略 params_model 显式校验 req.strategy_params（Pydantic 自动类型/范围校验）
    5. 实例化策略 → fit(price_data, macro) → generate_target_weights
    6. 全新 BacktestEngine.run_portfolio 执行回测（成本走 cost_model）
    7. 序列化结果为 BacktestResponse

    参数校验注入（反黑盒）：用 `params_model(**req.strategy_params)` 显式构造，
    禁 **kwargs 黑盒；非法参数在此处抛 ValidationError/ValueError，由路由层捕获。
    """
    from strategies.loader import StrategyLoader
    from strategies.base import StrategyContext

    # 默认策略：缺省 strategy_name 时取技术+宏观融合（与原 service 行为一致）
    DEFAULT_STRATEGY = "tech_macro_fusion"

    # ============ 步骤 1：取数 + 清洗 ============
    fetcher = MockDataFetcher(seed=DATA_DEFAULTS["mock_seed"])
    start_dt = datetime.combine(req.start_date, datetime.min.time())
    end_dt = datetime.combine(req.end_date, datetime.min.time())

    df = fetcher.fetch_ohlcv(req.symbol, start_dt, end_dt, freq=req.signal_freq)
    cleaner = DataCleaner()
    df_clean = cleaner.clean_ohlcv(df, max_fill=5)
    price_data = {req.symbol: df_clean}

    # 宏观数据（M2）——交由策略内部按 tech_weight 融合，对齐失败时策略自动退化为纯技术
    macro_df = fetcher.fetch_macro("m2", start_dt, end_dt)

    # ============ 步骤 2：选策略 + 校验注入参数 ============
    name = req.strategy_name or DEFAULT_STRATEGY
    loader = StrategyLoader()
    loader.scan()
    strategy_cls = loader.get(name)

    # 用策略的 params_model 校验请求参数（Pydantic 自动类型/范围校验）
    # 缺省 strategy_params → 用 params_model 默认值（如默认 MA 周期/融合权重）
    params = strategy_cls.params_model(**(req.strategy_params or {}))
    strategy = strategy_cls(universe=[req.symbol], params=params)

    # ============ 步骤 3：训练 + 产出信号 ============
    strategy.fit(price_data, macro_data=macro_df)
    ctx = StrategyContext(
        timestamp=start_dt,
        current_weights={req.symbol: 0.0},
        cash=req.initial_capital,
        aum=req.initial_capital,
    )
    signals = strategy.generate_target_weights(price_data, ctx)

    # ============ 步骤 4：执行回测 ============
    # 成本模型注入引擎（Task 6 已让其在 run_portfolio 路径生效）
    cost_model = _build_cost_model(req.cost_model)
    engine = BacktestEngine(initial_capital=req.initial_capital, cost_model=cost_model)
    result = engine.run_portfolio(price_data=price_data, signals=signals)

    # ============ 步骤 5：序列化 ============
    # 透传 price_data：序列化器需从中抽取 OHLCV 与持仓 symbol（引擎 daily_records
    # 不含开高低收量，必须由 price_data 旁路传入）。
    return _serialize_backtest_result(result, price_data)


def _build_cost_model(cost_params):
    """从请求的 CostModelParams 构造 CostModel（缺省用默认）

    Why 抽函数：原内联构造逻辑在 run_single_backtest 中重复，抽出后既
    消除重复，又让"成本可调不退化"的意图显式化（与 Task 6 测试呼应）。
    """
    if cost_params is None:
        return CostModel()
    return CostModel(
        commission_rate=cost_params.commission_rate,
        stamp_duty=cost_params.stamp_duty,
        min_commission=cost_params.min_commission,
        slippage_model=cost_params.slippage_model,
        slippage_rate=cost_params.slippage_rate,
        liquidity_threshold=cost_params.liquidity_threshold,
    )


def _extract_ohlcv(price_data: dict[str, pd.DataFrame]) -> list[OhlcvPoint]:
    """
    从 price_data 透传 OHLCV（单资产：取唯一 symbol 的 df）。

    ── 设计说明 ──
    - 列名沿用 data.fetcher 的小写英文（open/high/low/close/volume），不做任何
      数学变换，纯序列化。
    - 日期按 DataFrame 索引（Asia/Shanghai DatetimeIndex）strftime 为 ISO 字符串，
      与 nav_series / drawdown_series 的日期格式对齐。
    - 空数据短路：price_data 为空 / df 为 None / df.empty 时直接返回 []，防范
      后续 iloc 取列时 KeyError 或 IndexError。

    参数：
        price_data: {symbol: ohlcv_df}，由 run_single_backtest 第 84 行构造。

    返回：
        OhlcvPoint 列表（按 df 行序，保留全部交易日 K 线）。
    """
    if not price_data:
        return []
    # 单资产：取字典内唯一（或第一个）symbol 的 df
    df = next(iter(price_data.values()))
    if df is None or df.empty:
        return []
    # 向量化日期格式化（替代逐行 strftime）
    dates = df.index.strftime("%Y-%m-%d").tolist()
    # 列式提取，避免 iterrows 性能灾难
    opens = df["open"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    closes = df["close"].tolist()
    volumes = df["volume"].tolist()
    # ── 数值透传用 _safe_float（防 NaN/Inf 透传致非法 JSON）──
    # fetcher 通常干净，但停牌/缺失列场景下 OHLCV 仍可能出现 NaN；
    # JSON 规范不允许 NaN/Infinity，此处与 nav_series / metrics 路径保持一致的
    # 安全语义（NaN/Inf → 0.0），避免前端 JSON.parse 崩。
    points: list[OhlcvPoint] = []
    for i, d in enumerate(dates):
        points.append(
            OhlcvPoint(
                date=d,
                open=_safe_float(opens[i]),
                high=_safe_float(highs[i]),
                low=_safe_float(lows[i]),
                close=_safe_float(closes[i]),
                volume=_safe_float(volumes[i]),
            )
        )
    return points


def _extract_positions(daily_records: pd.DataFrame, symbol: str) -> list[PositionRow]:
    """
    取回测末态持仓快照（单资产：用末行 position / position_value）。

    ── 设计说明 ──
    - 仅取末行：持仓快照语义是"回测结束时的状态"，全量逐日持仓留给后续组合迭代。
    - market_value 优先取引擎已算好的 position_value 列；缺失（历史 daily 结构）
      时用 position * price 兜底，保证字段总有值。
    - 清仓短路：末态 qty=0 且 market_value=0 视为未持仓，返回 []（前端空表）。
    - 空数据短路：daily_records 为空时返回 []，防范 iloc[-1] IndexError。

    参数：
        daily_records: 引擎产出的逐日记录 DataFrame（含 position / position_value / price 列）。
        symbol: 单资产标的代码，用于 PositionRow.symbol 透传。

    返回：
        长度 0 或 1 的 PositionRow 列表（末态持仓快照）。
    """
    if daily_records is None or daily_records.empty:
        return []
    last = daily_records.iloc[-1]
    # ── 数值透传用 _safe_float（防 NaN/Inf 透传致非法 JSON）──
    # 引擎在极端行情 / 除零场景下可能写出 NaN/Inf 的 position / position_value，
    # 裸 float() 会原样透传进 PositionRow → FastAPI 产出非法 JSON → 前端 JSON.parse 崩。
    # 与 nav_series / metrics 路径保持一致的安全语义（NaN/Inf → 0.0）。
    # position 可能缺失（防御历史结构），统一 .get 兜底为 0。
    qty = _safe_float(last.get("position", 0) or 0)
    # 优先用引擎已算好的 position_value；缺失或为 None 时用 position*price 兜底。
    # 两路径均先对各操作数 _safe_float，再运算，确保 NaN/Inf 不污染最终 market_value。
    if "position_value" in daily_records.columns and last.get("position_value") is not None:
        mv = _safe_float(last["position_value"])
    else:
        price = _safe_float(last.get("price", 0) or 0)
        mv = _safe_float(qty * price)
    # 清仓 / 从未建仓 → 空列表（前端 PositionsTable 显示空态）
    if qty == 0 and mv == 0:
        return []
    return [PositionRow(symbol=symbol, qty=qty, market_value=mv)]


def _serialize_backtest_result(
    result: Dict[str, Any], price_data: dict[str, pd.DataFrame]
) -> BacktestResponse:
    """
    将引擎结果序列化为 BacktestResponse

    核心优化：
    - 从 daily_records (DataFrame) 中仅提取绘图必需的 4 个字段
    - 丢弃 cash / position / position_value / price / signal 等冗余列
    - 单独计算 drawdown_series（前端画回撤填充区需要）
    - 从 trades (DataFrame) 中仅提取 date/direction/shares/price/cost
    - NaN / Inf 替换为 None（JSON 安全），防范前端 JSON.parse 报错
    - 使用列式字典结构 (orient='list') 压缩传输体积

    参数：
        result: 引擎返回的原始结果字典
        price_data: {symbol: ohlcv_df}，由 run_single_backtest 第 84 行构造；
            用于透传 K 线（OHLCV）与推断持仓 symbol。

    返回：
        BacktestResponse（JSON 安全）
    """
    # ── 缺键防御（与 _serialize_portfolio_result 风格对称）──
    # Why：BacktestEngine._calculate_portfolio_result 在 daily_records 为空时走
    # 早返回路径，返回的字典缺 calmar_ratio / n_failed_trades / trades 等键，
    # 且 daily_records 是完全空（无列）的 DataFrame。若用 result["..."] 硬取键、
    # 或直接对空 DataFrame 做 nav_cols 列选择，空数据场景会 KeyError 崩；此处统一
    # .get 兜底 + 空 daily_records 短路，令空数据退化为合法的"全 0 + 空序列"响应，
    # 而非 500。
    daily_df: pd.DataFrame = result.get("daily_records", pd.DataFrame())
    trades_df: pd.DataFrame = result.get("trades", pd.DataFrame())

    # ── OHLCV / positions 透传（纯序列化，零数学逻辑）──
    # symbol：单资产取 price_data 唯一键；空字典时退化为 ""（PositionRow.symbol 兜底）。
    # ohlcv / positions 由两个 helper 统一处理空数据短路，此处不重复判空。
    symbol = next(iter(price_data), "")
    ohlcv = _extract_ohlcv(price_data)
    positions = _extract_positions(daily_df, symbol)

    # 空 daily_records 短路：早返回路径的 DataFrame 无 nav/return/cumulative_return 列，
    # 后续列选择会 KeyError。此处直接返回全 0 + 空序列响应（与 metrics 各 .get 兜底一致）。
    if len(daily_df) == 0:
        return BacktestResponse(
            metrics=MetricsResponse(
                initial_capital=_safe_float(result.get("initial_capital", 0.0)),
                final_nav=_safe_float(result.get("final_nav", 0.0)),
                total_return=_safe_float(result.get("total_return", 0.0)),
                annual_return=_safe_float(result.get("annual_return", 0.0)),
                annual_volatility=_safe_float(result.get("annual_volatility", 0.0)),
                max_drawdown=_safe_float(result.get("max_drawdown", 0.0)),
                sharpe_ratio=_safe_float(result.get("sharpe_ratio", 0.0)),
                calmar_ratio=_safe_float(result.get("calmar_ratio", 0.0)),
                win_rate=_safe_float(result.get("win_rate", 0.0)),
                profit_loss_ratio=_safe_float(result.get("profit_loss_ratio", 0.0)),
                n_trades=int(result.get("n_trades", 0)),
                n_failed_trades=int(result.get("n_failed_trades", 0)),
            ),
            nav_series=[],
            drawdown_series=[],
            trades=[],
            ohlcv=ohlcv,
            positions=positions,
        )

    # ============ 提取净值时序（精简 4 字段） ============
    # 仅保留绘图必需列，丢弃 cash/position/position_value/price/signal
    nav_cols = ["nav", "return", "cumulative_return"]
    nav_data = daily_df[nav_cols].copy()

    # ── NaN / Inf → None（JSON 安全） ──
    # JSON 规范不允许 NaN/Infinity，必须替换为 null
    # 使用 pd.DataFrame.where + np.isfinite 实现纯向量化替换，
    # 避免 iterrows 逐行判断的性能灾难
    nav_data = nav_data.where(np.isfinite(nav_data), None)

    # ── 列式字典结构（orient='list'）──
    # 对比 orient='records'：1000 行数据 → records 产生 1000 个对象，
    # list 只产生 3 个数组，体积压缩约 40%
    nav_dict = nav_data.to_dict(orient="list")

    # 日期序列单独提取（作为 x 轴共享索引）
    dates = [
        idx.strftime("%Y-%m-%d") if isinstance(idx, pd.Timestamp) else str(idx)
        for idx in daily_df.index
    ]

    # 构建精简的 NavPoint 列表（保持与 Pydantic 模型兼容）
    nav_series: list[NavPoint] = []
    for i in range(len(dates)):
        nav_series.append(NavPoint(
            date=dates[i],
            nav=nav_dict["nav"][i],
            return_=nav_dict["return"][i],
            cumulative_return=nav_dict["cumulative_return"][i],
        ))

    # ============ 计算回撤时序（纯向量化，无 iterrows） ============
    daily_returns = daily_df["nav"].pct_change().fillna(0.0)
    cumulative = (1 + daily_returns).cumprod()
    rolling_max = cumulative.expanding().max()
    drawdown = (cumulative - rolling_max) / rolling_max

    # 向量化 NaN/Inf → None 替换
    drawdown_safe = drawdown.where(np.isfinite(drawdown), None)

    drawdown_series: list[DrawdownPoint] = []
    for i, (idx, dd_val) in enumerate(drawdown_safe.items()):
        drawdown_series.append(DrawdownPoint(
            date=dates[i],
            drawdown=dd_val,
        ))

    # ============ 提取交易记录（精简 5 字段） ============
    trades: list[TradeRecord] = []
    if len(trades_df) > 0:
        # 仅保留绘图必需列
        trade_cols = ["date", "direction", "shares", "price", "cost"]
        trades_subset = trades_df[trade_cols].copy()

        # 向量化 NaN/Inf → None
        for col in ["price", "cost"]:
            trades_subset[col] = trades_subset[col].where(
                np.isfinite(trades_subset[col]), None
            )

        # 向量化日期格式化（替代逐行 strftime）
        trades_subset["date"] = pd.to_datetime(trades_subset["date"]).dt.strftime("%Y-%m-%d")

        # 列式提取，一次性构建列表
        trade_dates = trades_subset["date"].tolist()
        trade_dirs = trades_subset["direction"].tolist()
        trade_shares = trades_subset["shares"].astype(int).tolist()
        trade_prices = trades_subset["price"].tolist()
        trade_costs = trades_subset["cost"].tolist()

        for i in range(len(trade_dates)):
            trades.append(TradeRecord(
                date=trade_dates[i],
                direction=str(trade_dirs[i]),
                shares=trade_shares[i],
                price=trade_prices[i],
                cost=trade_costs[i],
            ))

    # ============ 构建响应 ============
    # Why .get 兜底：早返回路径（daily_records 空）返回的字典缺 calmar_ratio /
    # n_failed_trades / trades 等键，硬取键会 KeyError。此处全部改 .get 与
    # _serialize_portfolio_result 对称，空数据 → 全 0 兜底，避免 500。
    return BacktestResponse(
        metrics=MetricsResponse(
            initial_capital=_safe_float(result.get("initial_capital", 0.0)),
            final_nav=_safe_float(result.get("final_nav", 0.0)),
            total_return=_safe_float(result.get("total_return", 0.0)),
            annual_return=_safe_float(result.get("annual_return", 0.0)),
            annual_volatility=_safe_float(result.get("annual_volatility", 0.0)),
            max_drawdown=_safe_float(result.get("max_drawdown", 0.0)),
            sharpe_ratio=_safe_float(result.get("sharpe_ratio", 0.0)),
            calmar_ratio=_safe_float(result.get("calmar_ratio", 0.0)),
            # win_rate/profit_loss_ratio：run_portfolio 路径本就不返回这两个字段
            win_rate=_safe_float(result.get("win_rate", 0.0)),
            profit_loss_ratio=_safe_float(result.get("profit_loss_ratio", 0.0)),
            n_trades=int(result.get("n_trades", 0)),
            n_failed_trades=int(result.get("n_failed_trades", 0)),
        ),
        nav_series=nav_series,
        drawdown_series=drawdown_series,
        trades=trades,
        ohlcv=ohlcv,
        positions=positions,
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
