# -*- coding: utf-8 -*-
"""
单资产回测 Pydantic 模型

职责：
1. 定义请求参数的校验规则（类型、范围、必填）
2. 定义响应数据的序列化格式（剔除 DataFrame，仅保留 JSON 可序列化字段）

设计原则：
- 字段校验在 Pydantic 层完成，业务逻辑层无需重复校验
- 响应模型中的数值统一使用 float，避免 numpy.float64 序列化异常
- 日期字符串格式统一为 ISO 8601（YYYY-MM-DD）
"""
from datetime import date
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ============ 成本模型子模型 ============

class CostModelParams(BaseModel):
    """
    成本模型参数

    对应 backtest/cost_model.py 的 CostModel 构造参数，
    所有字段可选，缺省值与 config.py 一致。
    """
    commission_rate: float = Field(
        default=0.0003,
        ge=0.0, le=0.01,
        description="佣金率（默认万三）"
    )
    stamp_duty: float = Field(
        default=0.0005,
        ge=0.0, le=0.01,
        description="印花税率（默认千五，仅卖出）"
    )
    min_commission: float = Field(
        default=5.0,
        ge=0.0,
        description="最低佣金（元）"
    )
    slippage_model: str = Field(
        default="linear",
        description="滑点模型：linear / log"
    )
    slippage_rate: float = Field(
        default=0.001,
        ge=0.0, le=0.1,
        description="基础滑点率"
    )
    liquidity_threshold: float = Field(
        default=0.02,
        ge=0.0, le=1.0,
        description="流动性阈值"
    )

    @field_validator("slippage_model")
    @classmethod
    def validate_slippage_model(cls, v: str) -> str:
        """验证滑点模型仅支持 linear / log"""
        if v not in ("linear", "log"):
            raise ValueError(f"滑点模型仅支持 linear/log，当前: {v}")
        return v


# ============ 请求模型 ============

class BacktestRequest(BaseModel):
    """
    单资产回测请求

    校验规则：
    - initial_capital 必须为正数
    - start_date 必须早于 end_date
    - signal_freq 仅允许 "1d" / "1h" / "5m" / "1m"
    - strategy_name 缺省时由 service 层默认取 tech_macro_fusion
    - strategy_params 为宽松 dict，键值范围由对应策略的 params_model 在 service 层校验
      （Why：不同策略参数 schema 不同，请求层无法预知，必须延后到策略侧校验）
    """
    symbol: str = Field(
        ...,
        min_length=1,
        description="交易标的代码（如 600000.SH）"
    )
    start_date: date = Field(
        ...,
        description="回测起始日期"
    )
    end_date: date = Field(
        ...,
        description="回测结束日期"
    )
    initial_capital: float = Field(
        default=1_000_000,
        gt=0,
        description="初始资金（必须为正数）"
    )
    signal_freq: str = Field(
        default="1d",
        description="信号频率：1d/1h/5m/1m"
    )
    cost_model: Optional[CostModelParams] = Field(
        default=None,
        description="成本模型参数（可选，缺省使用默认值）"
    )
    strategy_name: Optional[str] = Field(
        default=None,
        description="策略名（对应 /api/v1/strategies 的 name）。缺省用默认策略 tech_macro_fusion"
    )
    strategy_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="策略参数（键值对，由对应策略的 params_model 在 service 层校验注入）"
    )

    @field_validator("signal_freq")
    @classmethod
    def validate_signal_freq(cls, v: str) -> str:
        """验证信号频率仅支持指定值"""
        allowed = {"1d", "1h", "5m", "1m"}
        if v not in allowed:
            raise ValueError(f"信号频率仅支持 {allowed}，当前: {v}")
        return v

    @model_validator(mode="after")
    def validate_date_range(self) -> "BacktestRequest":
        """验证起始日期必须早于结束日期"""
        if self.start_date >= self.end_date:
            raise ValueError("起始日期必须早于结束日期")
        return self


# ============ 响应模型 ============

class MetricsResponse(BaseModel):
    """核心绩效指标（从引擎结果中提取的 JSON 安全字段）"""
    initial_capital: float
    final_nav: float
    total_return: float
    annual_return: float
    annual_volatility: float
    max_drawdown: float
    sharpe_ratio: float
    calmar_ratio: float
    win_rate: float
    profit_loss_ratio: float
    n_trades: int
    n_failed_trades: int


class NavPoint(BaseModel):
    """
    精简净值时序节点（仅传输绘图必需字段，避免几十 MB 冗余数据）

    注意：return 是 Python 关键字，使用 return_ 作为字段名 + alias="return" 输出。
    populate_by_name=True 允许构造时使用 Python 字段名（return_），
    序列化时输出 JSON 键名 "return"。
    """
    model_config = ConfigDict(populate_by_name=True)

    date: str           # ISO 8601 格式 YYYY-MM-DD
    nav: float
    return_: float = Field(alias="return")
    cumulative_return: float


class DrawdownPoint(BaseModel):
    """回撤时序节点"""
    date: str
    drawdown: float


class TradeRecord(BaseModel):
    """精简交易记录（丢弃 amount/symbol 等冗余字段）"""
    date: str
    direction: str      # "buy" / "sell" / "failed"
    shares: int
    price: float
    cost: float


class OhlcvPoint(BaseModel):
    """
    单根 K 线（开高低收量），用于前端 ProChart 绘制蜡烛图。

    Why 单独建模：原 _serialize_backtest_result 丢弃了 OHLCV，前端无法画 K 线。
    本字段是纯透传——列名沿用 data.fetcher 的小写英文（open/high/low/close/volume），
    不做任何数学变换，仅把 DataFrame 行拍平为 JSON 安全的标量列表。
    """
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class PositionRow(BaseModel):
    """
    持仓快照行（取回测末态），用于前端 PositionsTable。

    单资产行为：仅取末行 position / position_value 一行。组合多资产的持仓快照
    作为后续迭代（PortfolioResponse），本轮不动——避免对单资产 BacktestResponse
    塞入语义不成立的多 symbol 持仓。
    """
    symbol: str
    qty: float
    market_value: float


class BacktestResponse(BaseModel):
    """单资产回测完整响应"""
    metrics: MetricsResponse
    nav_series: List[NavPoint]
    drawdown_series: List[DrawdownPoint]
    trades: List[TradeRecord]
    # K 线序列：前端 ProChart 蜡烛图数据源（data.fetcher OHLCV 透传）
    ohlcv: List[OhlcvPoint] = []
    # 末态持仓快照：前端 PositionsTable 数据源（仅末行，单资产）
    positions: List[PositionRow] = []
