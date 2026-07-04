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
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

import numpy as np
import pandas as pd

from data.fetcher import MockDataFetcher
from data.clients.akshare_client import AKShareClient
from data.lake_fetcher import LakeDataFetcher
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
    BenchmarkPoint,
)
from server.core.config import DATA_DEFAULTS

logger = logging.getLogger(__name__)


def run_single_backtest(
    req: BacktestRequest,
    event_emitter: Callable[[dict], None] | None = None,
) -> BacktestResponse:
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

    参数：
        req: 回测请求（Pydantic 校验后的对象）
        event_emitter: 可选 SSE 事件回调（默认 None → 零开销零行为变化）。
            Why 透传：SSE 实时流（GET /run/stream/{run_id}）需要从引擎逐日循环里
            拿到 progress/trade 帧。引擎层 run_portfolio 已支持 event_emitter 关键字，
            这里纯透传，不做任何中间加工（事件契约由引擎统一维护）。
            非 None 时，引擎会在每个交易日末尾调用 emitter({"type":...})。
    """
    from strategies.loader import StrategyLoader
    from strategies.base import StrategyContext

    # 默认策略：缺省 strategy_name 时取技术+宏观融合（与原 service 行为一致）
    DEFAULT_STRATEGY = "tech_macro_fusion"

    # ============ 步骤 1：取数 + 清洗（优先真实湖，离线降级 Mock）============
    # Why 双源：LakeDataFetcher 读 data_lake 真实历史；湖缺数据（开发机/CI 未同步）抛
    # LookupError → 降级 MockDataFetcher 保回测可跑，logger.warning 留痕。
    # 前视红线：不在取数层做 ffill/重采样（reader 返回原始时序，DataCleaner 统一清洗）。
    from data.lake_fetcher import LakeDataFetcher
    start_dt = datetime.combine(req.start_date, datetime.min.time())
    end_dt = datetime.combine(req.end_date, datetime.min.time())
    _lake = LakeDataFetcher()
    _mock = MockDataFetcher(seed=DATA_DEFAULTS["mock_seed"])

    try:
        df = _lake.fetch_ohlcv(req.symbol, start_dt, end_dt, freq=req.signal_freq)
        ohlcv_src = "data_lake"
    except LookupError as e:
        logger.warning("OHLCV 湖取数失败，降级 Mock：%s", e)
        df = _mock.fetch_ohlcv(req.symbol, start_dt, end_dt, freq=req.signal_freq)
        ohlcv_src = "mock"

    cleaner = DataCleaner()
    df_clean = cleaner.clean_ohlcv(df, max_fill=5)
    price_data = {req.symbol: df_clean}
    logger.info(
        "单资产取数 symbol=%s %s~%s freq=%s → %d 根K线（源=%s）",
        req.symbol, start_dt.date(), end_dt.date(), req.signal_freq, len(df_clean), ohlcv_src,
    )

    # 宏观数据（M2）——交由策略内部按 tech_weight 融合，对齐失败时策略自动退化为纯技术
    try:
        macro_df = _lake.fetch_macro("m2", start_dt, end_dt)
    except LookupError as e:
        logger.warning("macro 湖取数失败，M2 降级 Mock：%s", e)
        macro_df = _mock.fetch_macro("m2", start_dt, end_dt)

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
    logger.info("策略训练完成 strategy=%s", name)
    ctx = StrategyContext(
        timestamp=start_dt,
        current_weights={req.symbol: 0.0},
        cash=req.initial_capital,
        aum=req.initial_capital,
    )
    signals = strategy.generate_target_weights(price_data, ctx)

    # ============ 步骤 4：执行回测 ============
    # 成本模型注入引擎（Task 6 已让其在 run_portfolio 路径生效）
    # event_emitter 透传：SSE 实时流的核心——引擎逐日循环通过 emitter 推送
    # progress/trade 帧；默认 None 时引擎完全短路，零开销（与既有同步调用一致）。
    cost_model = _build_cost_model(req.cost_model)
    engine = BacktestEngine(initial_capital=req.initial_capital, cost_model=cost_model)
    result = engine.run_portfolio(
        price_data=price_data,
        signals=signals,
        event_emitter=event_emitter,
    )
    logger.info(
        "回测引擎执行完毕 n_trades=%d final_nav=%.2f",
        result.get("n_trades", 0), result.get("final_nav", 0.0),
    )

    # ============ 步骤 5：序列化 ============
    # 透传 price_data：序列化器需从中抽取 OHLCV 与持仓 symbol（引擎 daily_records
    # 不含开高低收量，必须由 price_data 旁路传入）。
    # 基准净值（沪深300 ETF 归一化）：按策略 nav 日期 reindex；缺数据返 [] 不崩
    strategy_dates = [
        idx.strftime("%Y-%m-%d") if isinstance(idx, pd.Timestamp) else str(idx)
        for idx in result.get("daily_records", pd.DataFrame()).index
    ]
    benchmark_series = _compute_benchmark_series(
        start_date=req.start_date, end_date=req.end_date,
        strategy_dates=strategy_dates,
    )
    return _serialize_backtest_result(result, price_data, benchmark_series=benchmark_series)


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


def _extract_positions(
    daily_records: pd.DataFrame,
    symbol: str,
    trades_df: pd.DataFrame | None = None,
) -> list[PositionRow]:
    """
    取回测末态持仓快照（单资产）。

    适配两条引擎路径的 daily_records 结构（缺一即 qty 错乱）：
    - run_portfolio（组合路径，单资产回测实际走此）：daily_records 含 positions dict
      {symbol: 股数} 与 position_values dict {symbol: 市值}，【无】position 标量列。
    - run（历史单资产路径）：daily_records 含 position 标量列 + position_value/price。

    Why 优先 dict：单资产回测经 service → engine.run_portfolio，daily_records 是组合结构，
    若仍按 position 标量列取值会得到 0（列不存在），mv 又误取 position_value 标量（总市值），
    表现为「0 股 + 15 万市值」的错乱快照。dict 路径按 symbol 精确取股数与该 symbol 市值。

    参数：
        daily_records: 引擎产出的逐日记录 DataFrame。
        symbol: 单资产标的代码，用于从 positions/position_values dict 取该 symbol 的值。

    返回：
        长度 0 或 1 的 PositionRow 列表（末态持仓快照）。
    """
    if daily_records is None or daily_records.empty:
        return []
    last = daily_records.iloc[-1]

    positions_dict = last.get("positions")
    position_values_dict = last.get("position_values")

    if isinstance(positions_dict, dict) and symbol in positions_dict:
        # run_portfolio 路径（当前主路径）：从 dict 按 symbol 取股数与市值
        qty = _safe_float(positions_dict.get(symbol, 0))
        mv = _safe_float(position_values_dict.get(symbol, 0)) if isinstance(position_values_dict, dict) else 0.0
    elif "position" in daily_records.columns:
        # 历史 run 路径：用 position 标量列 + position_value/price 兜底（向后兼容）
        qty = _safe_float(last.get("position", 0) or 0)
        if "position_value" in daily_records.columns and last.get("position_value") is not None:
            mv = _safe_float(last["position_value"])
        else:
            price = _safe_float(last.get("price", 0) or 0)
            mv = _safe_float(qty * price)
    else:
        qty = 0.0
        mv = 0.0

    # 清仓 / 从未建仓 → 空列表（前端 PositionsTable 显示空态）
    if qty == 0 and mv == 0:
        return []

    # ── 详情字段：成本/盈亏/时间/资产 ──
    avg_cost, open_date = _compute_cost_basis(trades_df)
    cost_total = qty * avg_cost
    pnl = mv - cost_total
    pnl_pct = (pnl / cost_total) if cost_total > 0 else 0.0
    holding_days = 0
    open_date_str: str | None = None
    if open_date is not None:
        open_date_str = open_date.strftime("%Y-%m-%d")
        try:
            end_date = pd.Timestamp(daily_records.index[-1])
            holding_days = int((end_date - pd.Timestamp(open_date)).days)
        except Exception:
            holding_days = 0
    cash = _safe_float(last.get("cash", 0) or 0)
    nav = _safe_float(last.get("nav", 0) or 0)

    return [PositionRow(
        symbol=symbol,
        qty=qty,
        market_value=mv,
        avg_cost=avg_cost,
        unrealized_pnl=pnl,
        unrealized_pnl_pct=pnl_pct,
        open_date=open_date_str,
        holding_days=holding_days,
        cash=cash,
        nav=nav,
    )]


def _compute_cost_basis(
    trades_df: pd.DataFrame | None,
) -> tuple[float, Optional[pd.Timestamp]]:
    """从 trades 加权平均算末态持仓成本与首笔建仓日期。

    加权平均法：buy 累加成本与股数（avg = 累计成本 / 累计股数），sell 按当前 avg
    减成本与股数。返回 (avg_cost, 首笔 buy 日期)。无 trades / 未建仓 → (0.0, None)。

    Why 加权平均而非 FIFO：A 股单一标的调仓场景下，加权平均与 FIFO 末态成本接近，
    且实现直白（单趟遍历、O(n)），符合极简原则；FIFO 需维护分批队列，复杂度不值得。
    """
    if trades_df is None or len(trades_df) == 0:
        return 0.0, None
    # 按 date 排序保证交易时序正确（加权平均依赖顺序）
    df = trades_df.sort_values("date") if "date" in trades_df.columns else trades_df
    cost_basis = 0.0   # 当前持仓的成本总额
    qty = 0.0           # 当前持仓股数
    avg_cost = 0.0
    first_buy_date: Optional[pd.Timestamp] = None
    for _, t in df.iterrows():
        direction = t.get("direction")
        shares = _safe_float(t.get("shares", 0))
        price = _safe_float(t.get("price", 0))
        if direction == "buy" and shares > 0:
            cost_basis += shares * price
            qty += shares
            avg_cost = cost_basis / qty if qty > 0 else 0.0
            if first_buy_date is None:
                try:
                    first_buy_date = pd.Timestamp(t.get("date"))
                except Exception:
                    pass
        elif direction == "sell" and shares > 0:
            # 卖出按当前 avg_cost 减成本与股数（加权平均法）
            cost_basis -= shares * avg_cost
            qty -= shares
            if qty <= 0:
                qty = 0.0
                cost_basis = 0.0
                avg_cost = 0.0
    return avg_cost, first_buy_date


_BENCHMARK_SYMBOL = "510300.SH"


def _compute_benchmark_series(start_date, end_date, strategy_dates: list) -> list:
    """计算沪深300 ETF 基准累计净值（归一化起点 1.0，按策略日期 reindex + 前向填充）。

    取数三级降级（绝不抛）：
      1) LakeDataFetcher.fetch_ohlcv("510300.SH", daily 湖) —— 与策略同协议
      2) AKShareClient.fetch_daily_hist 在线兜底（带熔断）
      3) 全空 → 返 []（ProChart 不画基准线，降级不崩）

    归一化：close / close[0]（首值），起点 = 1.0。
    对齐：基准按 strategy_dates reindex，缺失日前向填充（基准停牌日沿用前收）。
    NaN 守卫：close 列 dropna 后归一化；strategy_dates 为空 → 返 []。

    参数：
        start_date/end_date: datetime.date，回测区间（取数窗口）。
        strategy_dates: 策略 nav_series 的日期字符串列表（YYYY-MM-DD），基准按此 reindex。
    """
    if not strategy_dates:
        return []

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.min.time())

    # 三级降级取 close Series
    close = pd.Series(dtype=float)
    try:
        df = LakeDataFetcher().fetch_ohlcv(_BENCHMARK_SYMBOL, start_dt, end_dt, freq="1d")
        if df is not None and not df.empty and "close" in df.columns:
            close = df["close"].dropna()
    except LookupError as e:
        logger.info("基准 daily 湖取数失败，降级 AKShare：%s", e)
    except Exception as e:
        logger.warning("基准 daily 湖取数异常：%s", e)

    if close.empty:
        try:
            ak_df = AKShareClient().fetch_daily_hist(
                _BENCHMARK_SYMBOL,
                start_date.strftime("%Y%m%d"),
                end_date.strftime("%Y%m%d"),
                adjust="qfq",
            )
            if ak_df is not None and not ak_df.empty and "close" in ak_df.columns:
                close = ak_df["close"].dropna()
        except Exception as e:
            logger.warning("基准 AKShare 在线取数失败（降级空基准）：%s", e)

    if close.empty:
        return []  # 三级全空 → 不画基准线

    # 归一化到起点 1.0
    first = close.iloc[0]
    if first == 0 or not np.isfinite(first):
        return []
    nav = close / first

    # 按策略日期 reindex + 前向填充（基准日期索引归一为 YYYY-MM-DD 字符串）
    nav.index = nav.index.strftime("%Y-%m-%d")
    aligned = nav.reindex(strategy_dates).ffill()

    return [
        BenchmarkPoint(date=d, nav=float(v))
        for d, v in aligned.items()
        if pd.notna(v)
    ]


def _serialize_backtest_result(
    result: Dict[str, Any],
    price_data: dict[str, pd.DataFrame],
    benchmark_series: list | None = None,
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
    benchmark_series = benchmark_series or []
    daily_df: pd.DataFrame = result.get("daily_records", pd.DataFrame())
    trades_df: pd.DataFrame = result.get("trades", pd.DataFrame())

    # ── OHLCV / positions 透传（纯序列化，零数学逻辑）──
    # symbol：单资产取 price_data 唯一键；空字典时退化为 ""（PositionRow.symbol 兜底）。
    # ohlcv / positions 由两个 helper 统一处理空数据短路，此处不重复判空。
    symbol = next(iter(price_data), "")
    ohlcv = _extract_ohlcv(price_data)
    positions = _extract_positions(daily_df, symbol, trades_df)

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
            benchmark_series=benchmark_series,
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
        # 每个数值经 _safe_float：NaN/Inf → 0.0，兑现上方「NaN/Inf → null」的安全语义。
        # Why 不依赖 nav_data.where(np.isfinite, None)：pandas float 列中 None 会被
        # 自动转回 NaN（float dtype 不支持 NA，None→NaN），该 where 对数值列无效；
        # 必须在标量出口处用 _safe_float 兜底，否则 NaN 流入 SSE result 帧，经
        # json.dumps(allow_nan=True) 输出字面 NaN → 浏览器 JSON.parse 失败、K 线不显示。
        nav_series.append(NavPoint(
            date=dates[i],
            nav=_safe_float(nav_dict["nav"][i]),
            return_=_safe_float(nav_dict["return"][i]),
            cumulative_return=_safe_float(nav_dict["cumulative_return"][i]),
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
    logger.info(
        "序列化完成 ohlcv=%d nav=%d drawdown=%d trades=%d positions=%d",
        len(ohlcv), len(nav_series), len(drawdown_series), len(trades), len(positions),
    )
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
        benchmark_series=benchmark_series,
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
        # None/str 等「正常缺失」（如 position 字段缺失）静默归零，不打扰
        return 0.0

    # 防范 NaN / Inf（JSON 规范不允许）
    if not np.isfinite(f):
        # NaN/Inf 是脏数据（如未清洗的 pct_change 首行），留痕便于定位源头
        logger.warning("_safe_float 拦截到 NaN/Inf 已归零：原始值=%r", val)
        return 0.0

    return f
