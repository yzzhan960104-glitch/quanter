# 模块① 策略插件系统 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立动态策略插件系统 `strategies/`：`BaseStrategy` 抽象基类 + `StrategyLoader`（importlib 动态扫描）+ HMM/双均线两个策略 + `/api/v1/strategies` 接口；并下线单资产 Series 路径，让所有回测统一走 `run_portfolio`。

**Architecture:** 所有策略只产出 `List[TargetWeightSignal]`（权重信号）。单资产视为单标的组合退化。引擎只保留 `run_portfolio` 一条路径。StrategyLoader 启动时扫描 `strategies/` 白名单目录，按 `name` 属性注册。回测/实盘走同一条"策略→引擎→风控→broker"链。

**Tech Stack:** Python 3, pandas, numpy, importlib（标准库）, pytest；FastAPI（API 任务）；复用现有 `factors.fusion.TargetWeightSignal/SignalDirection`、`factors.hmm_macro.MacroRegimeHMM`、`factors.fusion.HMMStateMapper`、`backtest.engine.BacktestEngine`。

## Global Constraints

（摘自 spec 与 CLAUDE.md）

- 严禁 PyQt/GUI；全中文注释（含 Why）；扁平反黑盒，不引入策略框架第三方库
- 策略约定"fit 后只读"；并发回测由 service 层每请求 new 一个策略实例（与 engine 同生命周期）
- StrategyLoader 只扫描 `strategies/` 白名单目录，要求类显式声明 `name` 才注册（防恶意/隐式加载）
- 向后兼容：`/api/v1/backtest`、`/portfolio` 路由入参出参结构不变；前端无需改
- **本模块依赖模块② MyTT（已完成）**：`MaCrossStrategy` 调用 `factors.mytt.MACD`

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `factors/fusion.py` | 放宽 `TargetWeightSignal` 权重和约束（允许 ≤1，现金为隐含剩余） | 修改（`__post_init__`） |
| `strategies/__init__.py` | 策略包 | 新建 |
| `strategies/base.py` | `BaseStrategy` 抽象基类 + `StrategyContext` | 新建 |
| `strategies/ma_cross_strategy.py` | 单标的 MACD 示例策略（用 MyTT） | 新建 |
| `strategies/hmm_macro_strategy.py` | HMM 宏观策略（改写自 portfolio_service） | 新建 |
| `strategies/loader.py` | `StrategyLoader`（importlib 扫描） | 新建 |
| `server/api/v1/strategies.py` | `GET /api/v1/strategies` | 新建 |
| `server/main.py` | lifespan 启动扫描策略 | 修改 |
| `server/schemas/backtest.py` | `BacktestRequest` 增 `strategy_name` | 修改 |
| `server/services/backtest_service.py` | 下线 Series 路径，走策略 + run_portfolio | 修改 |
| `server/services/portfolio_service.py` | HMM 逻辑迁入策略后瘦身 | 修改 |
| `tests/test_strategy.py` | 策略系统测试 | 新建 |
| `tests/test_fusion.py` 或 `tests/test_factors.py` | TargetWeightSignal 约束放宽测试 | 追加 |

---

## Task 1: 放宽 `TargetWeightSignal` 权重和约束 + `strategies/base.py`

**Files:**
- Modify: `factors/fusion.py:262-274`（`TargetWeightSignal.__post_init__`）
- Create: `strategies/__init__.py`、`strategies/base.py`
- Test: `tests/test_factors.py`（追加 `TestTargetWeightSignalSum`）

**Interfaces:**
- Consumes: `factors.fusion.TargetWeightSignal`（现有）
- Produces: `strategies.base.BaseStrategy`、`strategies.base.StrategyContext`（供 Task 2/3 策略继承）

**为何先做这一步**：现有 `__post_init__` 强制 `weights 和 == 1`，但单资产 50% 仓位 = `{symbol: 0.5}` 和为 0.5 会被拒。统一信号语义要求允许部分仓位（现金作隐含剩余），故先放宽。现有组合策略 sum=1 仍合法（≤1），向后兼容。

- [ ] **Step 1: 写失败测试**

在 `tests/test_factors.py` 末尾追加（import 区已有 `from factors import ... TargetWeightSignal`，若无则补 `from factors.fusion import TargetWeightSignal`）：

```python
class TestTargetWeightSignalSum:
    """测试 TargetWeightSignal 权重和约束放宽（允许 ≤1，现金为隐含剩余）"""

    def _sig(self, weights):
        from factors.fusion import TargetWeightSignal, SignalDirection
        return TargetWeightSignal(
            timestamp=pd.Timestamp("2023-01-01"),
            weights=weights,
            directions={k: SignalDirection.BUY for k in weights},
        )

    def test_sum_equals_one_still_valid(self):
        """权和=1（满仓组合）仍合法"""
        sig = self._sig({"510300.SH": 0.8, "511010.SH": 0.2})
        assert sig.weights["510300.SH"] == 0.8

    def test_sum_less_than_one_valid(self):
        """权和<1（部分仓位，现金剩余）合法 —— 单资产退化场景"""
        sig = self._sig({"600000.SH": 0.5})
        assert sig.weights["600000.SH"] == 0.5

    def test_sum_zero_valid(self):
        """权和=0（全空仓）合法"""
        sig = self._sig({"600000.SH": 0.0})
        assert sig.weights["600000.SH"] == 0.0

    def test_sum_above_one_rejected(self):
        """权和>1 拒绝（超额配置，物理不可能）"""
        with pytest.raises(ValueError, match="超出"):
            self._sig({"600000.SH": 1.5})

    def test_negative_weight_rejected(self):
        """负权重拒绝（纯多头，不做空）"""
        with pytest.raises(ValueError, match="超出"):
            self._sig({"600000.SH": -0.1})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_factors.py::TestTargetWeightSignalSum -v`
Expected: `test_sum_less_than_one_valid`、`test_sum_zero_valid` FAIL（现有约束要求和==1）

- [ ] **Step 3a: 放宽 `factors/fusion.py` 的 `__post_init__`**

把 `factors/fusion.py` 中 `TargetWeightSignal.__post_init__` 的权重和校验段（约 271-274 行）：
```python
        # 验证权重和为 1（允许浮点误差）
        weight_sum = sum(self.weights.values())
        if not np.isclose(weight_sum, 1.0, atol=1e-6):
            raise ValueError(f"权重和不等于 1: {weight_sum:.6f}")
```
改为：
```python
        # 验证权重和在 [0, 1]（允许部分仓位，现金为隐含剩余）
        # 放宽原因：单资产=单标的组合退化时，50% 仓位 = {symbol: 0.5}，和为 0.5。
        # 现金（1 - 权重和）作为隐含资产持有，无需显式标的。
        # 现有组合策略 sum=1 仍合法（满仓），向后兼容。
        weight_sum = sum(self.weights.values())
        if weight_sum < -1e-8 or weight_sum > 1.0 + 1e-8:
            raise ValueError(
                f"权重和超出 [0,1] 范围（现金为隐含剩余）: {weight_sum:.6f}"
            )
```

- [ ] **Step 3b: 新建 `strategies/__init__.py`**

```python
"""策略插件包

设计原则：
- 每个策略一个模块，继承 BaseStrategy
- StrategyLoader 启动时 importlib 扫描本目录自动注册
- 策略只产出 List[TargetWeightSignal]，与引擎/风控/broker 解耦
"""
```

- [ ] **Step 3c: 新建 `strategies/base.py`**

```python
"""策略抽象基类与运行时上下文

统一契约：所有策略实现 fit（训练）+ generate_target_weights（产出权重信号）。
单资产策略 = 单标的组合的退化（universe 仅 1 个标的）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional, Any

import pandas as pd

from factors.fusion import TargetWeightSignal


@dataclass
class StrategyContext:
    """策略运行时只读快照

    防策略误改账户状态：策略只能读 ctx，不能持有/修改引擎的可变账户。
    current_weights 由引擎在调用前注入（迟滞滤波/方向判定基准）。

    属性：
        timestamp: 当前信号时间戳
        current_weights: 当前实际权重 {symbol: weight}
        cash: 可用现金
        aum: 账户总市值
        params: 策略参数（来自请求/默认配置）
    """
    timestamp: pd.Timestamp
    current_weights: Dict[str, float] = field(default_factory=dict)
    cash: float = 0.0
    aum: float = 0.0
    params: Dict[str, Any] = field(default_factory=dict)


class BaseStrategy(ABC):
    """策略抽象基类

    子类必须声明 ClassVar：
        name: 策略唯一标识（StrategyLoader 注册 key、前端下拉框 value，必填）
        universe: 标的池

    约定：fit 后实例进入只读状态；并发回测时每请求 new 一个实例。
    """

    name: ClassVar[str]
    universe: ClassVar[List[str]]

    @abstractmethod
    def fit(
        self,
        price_data: Dict[str, pd.DataFrame],
        macro_data: Optional[pd.DataFrame] = None,
    ) -> None:
        """训练阶段（如 HMM 训练）。无状态策略实现为 pass"""

    @abstractmethod
    def generate_target_weights(
        self,
        price_data: Dict[str, pd.DataFrame],
        ctx: StrategyContext,
    ) -> List[TargetWeightSignal]:
        """产出每日目标权重信号序列（复用 TargetWeightSignal，不新造类型）"""
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_factors.py::TestTargetWeightSignalSum -v`
Expected: PASS — 5 个测试全绿

- [ ] **Step 5: 回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿（确认放宽约束未破坏现有组合回测——它们 sum=1 仍合法）

- [ ] **Step 6: 提交**

```bash
git add factors/fusion.py strategies/__init__.py strategies/base.py tests/test_factors.py
git commit -m "feat(strategies): 放宽 TargetWeightSignal 权重和约束 + 新增 BaseStrategy"
```

---

## Task 2: `MaCrossStrategy`（单标的 MACD 示例策略）

**Files:**
- Create: `strategies/ma_cross_strategy.py`
- Test: `tests/test_strategy.py`（新建）

**Interfaces:**
- Consumes: `BaseStrategy`/`StrategyContext`（Task 1）、`factors.mytt.MACD`（模块②）
- Produces: `MaCrossStrategy`（name="ma_cross"，universe 在构造时传入）

**实现要点**：MACD 金叉/死叉 → [0,1] 仓位 → `{symbol: weight}`。direction 设为非 HOLD（BUY）仅为让引擎纳入调仓评估集合；实际买卖由引擎按 delta 符号 + 整手过滤决定，与原 `run()` 逐日重算语义一致。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_strategy.py`：
```python
"""策略插件系统单元测试"""
import numpy as np
import pandas as pd
import pytest

from factors.fusion import TargetWeightSignal, SignalDirection
from strategies.base import BaseStrategy, StrategyContext
from strategies.ma_cross_strategy import MaCrossStrategy


@pytest.fixture
def single_price_data():
    """单标的 100 日 OHLCV"""
    symbol = "600000.SH"
    dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
    np.random.seed(42)
    prices = 100 + np.cumsum(np.random.randn(100))
    df = pd.DataFrame({
        "open": prices, "high": prices + 1, "low": prices - 1,
        "close": prices, "volume": 1e6, "amount": 1e8,
    }, index=dates)
    return {symbol: df}


class TestMaCrossStrategy:
    """测试 MACD 双均线策略"""

    def test_is_base_strategy(self):
        """MaCrossStrategy 是 BaseStrategy 子类"""
        strat = MaCrossStrategy(universe=["600000.SH"])
        assert isinstance(strat, BaseStrategy)

    def test_has_name(self):
        """声明了 name 类属性"""
        assert MaCrossStrategy.name == "ma_cross"

    def test_fit_is_noop(self, single_price_data):
        """fit 为无操作（无状态策略）"""
        strat = MaCrossStrategy(universe=["600000.SH"])
        strat.fit(single_price_data)  # 不应抛异常

    def test_generate_returns_target_weight_signals(self, single_price_data):
        """generate_target_weights 返回 List[TargetWeightSignal]"""
        strat = MaCrossStrategy(universe=["600000.SH"])
        ctx = StrategyContext(
            timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"),
            current_weights={"600000.SH": 0.0},
        )
        signals = strat.generate_target_weights(single_price_data, ctx)

        assert isinstance(signals, list)
        assert len(signals) > 0
        assert all(isinstance(s, TargetWeightSignal) for s in signals)

    def test_weights_in_zero_one_range(self, single_price_data):
        """权重在 [0,1] 范围（纯多头）"""
        strat = MaCrossStrategy(universe=["600000.SH"])
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(single_price_data, ctx)

        for s in signals:
            for w in s.weights.values():
                assert 0.0 <= w <= 1.0

    def test_direction_not_all_hold(self, single_price_data):
        """至少部分日 direction 非 HOLD（否则引擎永不调仓）"""
        strat = MaCrossStrategy(universe=["600000.SH"])
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(single_price_data, ctx)

        non_hold = [s for s in signals
                    if s.directions["600000.SH"] != SignalDirection.HOLD]
        assert len(non_hold) > 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'strategies.ma_cross_strategy'`

- [ ] **Step 3: 写最小实现**

新建 `strategies/ma_cross_strategy.py`：
```python
"""MACD 双均线策略（单标的示例，演示 BaseStrategy + MyTT 用法）

策略逻辑（与原 factors/technical.py.macd 的金叉/死叉一致）：
- MACD 金叉（DIF 上穿 DEA）→ 满仓（weight=1.0）
- MACD 死叉（DIF 下穿 DEA）→ 空仓（weight=0.0）
- 持仓状态（DIF>DEA）→ 维持（前值 ffill）
"""
from typing import ClassVar, Dict, List, Optional

import pandas as pd

from factors.fusion import TargetWeightSignal, SignalDirection
from factors.mytt import MACD
from .base import BaseStrategy, StrategyContext


class MaCrossStrategy(BaseStrategy):
    """单标的 MACD 金叉/死叉策略"""

    name: ClassVar[str] = "ma_cross"
    universe: ClassVar[List[str]]  # 构造时注入

    def __init__(
        self,
        universe: List[str],
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ):
        """
        参数：
            universe: 单标的列表（取首个）
            fast/slow/signal: MACD 周期
        """
        # ClassVar 通过实例属性覆盖（注册时 loader 读类属性，故同步设置）
        self.universe = list(universe)
        self._fast = fast
        self._slow = slow
        self._signal = signal
        self._symbol = self.universe[0]

    def fit(
        self,
        price_data: Dict[str, pd.DataFrame],
        macro_data: Optional[pd.DataFrame] = None,
    ) -> None:
        """无状态策略，无需训练"""
        return None

    def generate_target_weights(
        self,
        price_data: Dict[str, pd.DataFrame],
        ctx: StrategyContext,
    ) -> List[TargetWeightSignal]:
        """MACD 金叉/死叉 → 目标权重信号序列"""
        df = price_data[self._symbol]
        dif, dea, _ = MACD(df["close"], self._fast, self._slow, self._signal)

        # 金叉/死叉判定（shift(1) 防前视偏差）
        golden = (dif.shift(1) < dea.shift(1)) & (dif > dea)
        death = (dif.shift(1) > dea.shift(1)) & (dif < dea)

        weight = pd.Series(0.5, index=df.index)   # 默认半仓（中性）
        weight[golden] = 1.0
        weight[death] = 0.0
        # 持仓状态：DIF>DEA 维持前值
        holding = (dif > dea) & ~golden & ~death
        weight[holding] = weight[holding].shift(1)
        weight = weight.ffill().fillna(0.0)
        weight = weight.clip(0.0, 1.0)

        signals: List[TargetWeightSignal] = []
        for ts, w in weight.items():
            # direction 设为 BUY（非 HOLD）使引擎纳入调仓评估；
            # 实际买卖由引擎按 delta 符号 + 整手过滤决定
            signals.append(TargetWeightSignal(
                timestamp=ts,
                weights={self._symbol: float(w)},
                directions={self._symbol: SignalDirection.BUY},
            ))
        return signals
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py -v`
Expected: PASS — 6 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add strategies/ma_cross_strategy.py tests/test_strategy.py
git commit -m "feat(strategies): 新增 MaCrossStrategy 单标的 MACD 策略"
```

---

## Task 3: `HMMMacroStrategy`（HMM 宏观策略，改写自 portfolio_service）

**Files:**
- Create: `strategies/hmm_macro_strategy.py`
- Test: `tests/test_strategy.py`（追加 `TestHMMMacroStrategy`）

**Interfaces:**
- Consumes: `BaseStrategy`（Task 1）、`factors.hmm_macro.MacroRegimeHMM`、`factors.fusion.HMMStateMapper/AssetWeightConfig`
- Produces: `HMMMacroStrategy`（name="hmm_macro"），封装现有 portfolio_service 步骤 2-5

**实现要点**：`fit` 做 HMM 训练（对齐→fit→predict 得概率矩阵）；`generate_target_weights` 用 mapper 把概率矩阵映射为权重信号。逻辑整体搬迁自 `portfolio_service.run_portfolio_backtest` 步骤 2-5。

- [ ] **Step 1: 写失败测试**

在 `tests/test_strategy.py` 追加（import 区补）：
```python
from strategies.hmm_macro_strategy import HMMMacroStrategy
```

```python
@pytest.fixture
def multi_price_data():
    """双标的 OHLCV + 宏观"""
    symbols = ["510300.SH", "511010.SH"]
    dates = pd.date_range("2022-01-01", periods=300, freq="D", tz="Asia/Shanghai")
    np.random.seed(0)
    data = {}
    for s in symbols:
        prices = 100 + np.cumsum(np.random.randn(300))
        data[s] = pd.DataFrame({
            "open": prices, "high": prices + 1, "low": prices - 1,
            "close": prices, "volume": 1e6, "amount": 1e8,
        }, index=dates)
    return data


class TestHMMMacroStrategy:
    """测试 HMM 宏观策略"""

    STATE_WEIGHTS = {
        "State_0": {"510300.SH": 0.8, "511010.SH": 0.2},
        "State_1": {"510300.SH": 0.2, "511010.SH": 0.8},
        "State_2": {"510300.SH": 0.5, "511010.SH": 0.5},
    }

    def _make(self):
        return HMMMacroStrategy(
            universe=["510300.SH", "511010.SH"],
            n_hmm_states=3,
            state_weights=self.STATE_WEIGHTS,
            buffer_threshold=0.05,
        )

    def test_has_name(self):
        assert HMMMacroStrategy.name == "hmm_macro"

    def test_fit_then_generate(self, multi_price_data):
        """fit 后能 generate 出信号"""
        strat = self._make()
        macro = pd.DataFrame(
            {"m2": np.linspace(200, 220, 25)},
            index=pd.date_range("2022-01-01", periods=25, freq="MS", tz="Asia/Shanghai"),
        )
        strat.fit(multi_price_data, macro_data=macro)

        ctx = StrategyContext(timestamp=pd.Timestamp("2022-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(multi_price_data, ctx)

        assert len(signals) > 0
        assert all(isinstance(s, TargetWeightSignal) for s in signals)

    def test_weights_cover_universe(self, multi_price_data):
        """每个信号覆盖全部 universe 标的"""
        strat = self._make()
        macro = pd.DataFrame(
            {"m2": np.linspace(200, 220, 25)},
            index=pd.date_range("2022-01-01", periods=25, freq="MS", tz="Asia/Shanghai"),
        )
        strat.fit(multi_price_data, macro_data=macro)
        ctx = StrategyContext(timestamp=pd.Timestamp("2022-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(multi_price_data, ctx)

        for s in signals:
            assert set(s.weights.keys()) == {"510300.SH", "511010.SH"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py::TestHMMMacroStrategy -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 写最小实现**

新建 `strategies/hmm_macro_strategy.py`：
```python
"""HMM 宏观状态识别 + 迟滞调仓策略

逻辑搬迁自 server/services/portfolio_service.run_portfolio_backtest 步骤 2-5：
对齐宏观数据 → 训练 HMM → 预测状态概率 → mapper 映射为目标权重信号。
封装为策略后，service 层只负责取数 + 实例化 + run_portfolio。
"""
from typing import ClassVar, Dict, List, Optional

import pandas as pd

from factors.fusion import (
    HMMStateMapper, AssetWeightConfig, TargetWeightSignal,
)
from factors.hmm_macro import MacroRegimeHMM
from .base import BaseStrategy, StrategyContext


class HMMMacroStrategy(BaseStrategy):
    """HMM 宏观状态 → ETF 权重（含迟滞滤波）"""

    name: ClassVar[str] = "hmm_macro"
    universe: ClassVar[List[str]]

    def __init__(
        self,
        universe: List[str],
        n_hmm_states: int = 3,
        state_weights: Optional[Dict[str, Dict[str, float]]] = None,
        buffer_threshold: float = 0.05,
        covariance_type: str = "diag",
        n_iter: int = 100,
        random_state: int = 42,
    ):
        self.universe = list(universe)
        self._n_states = n_hmm_states
        self._state_weights = state_weights or {}
        self._buffer = buffer_threshold

        self._hmm = MacroRegimeHMM(
            n_components=n_hmm_states,
            covariance_type=covariance_type,
            n_iter=n_iter,
            random_state=random_state,
        )
        self._mapper: Optional[HMMStateMapper] = None
        self._prob_matrix: Optional[pd.DataFrame] = None

    def fit(
        self,
        price_data: Dict[str, pd.DataFrame],
        macro_data: Optional[pd.DataFrame] = None,
    ) -> None:
        """对齐 + 训练 HMM + 预测概率矩阵"""
        if macro_data is None or macro_data.empty:
            raise ValueError("HMM 宏观策略需要宏观数据（macro_data）")

        base = self.universe[0]
        daily_df = price_data[base][["close"]].rename(columns={"close": f"{base}_close"})
        for s in self.universe[1:]:
            if s in price_data:
                daily_df[f"{s}_close"] = price_data[s]["close"]

        # 对齐宏观数据（严格防未来函数）
        aligned = self._hmm.align_macro_data(
            daily_df.dropna(), macro_data, release_lag=5, max_fill_days=90,
        )
        feature_cols = [c for c in aligned.columns if not c.endswith("_freshness")]
        self._hmm.fit(aligned, feature_columns=feature_cols, drop_na=True)
        self._prob_matrix, _ = self._hmm.predict(aligned, drop_na=False)

        # 初始化 mapper（每次 fit 重置，防跨请求状态污染）
        assets = [AssetWeightConfig(symbol=s, base_name=s) for s in self.universe]
        self._mapper = HMMStateMapper(
            states=self._n_states,
            assets=assets,
            state_weights=self._state_weights,
            buffer_threshold=self._buffer,
        )

    def generate_target_weights(
        self,
        price_data: Dict[str, pd.DataFrame],
        ctx: StrategyContext,
    ) -> List[TargetWeightSignal]:
        """概率矩阵 → 目标权重信号（迟滞滤波）"""
        if self._mapper is None or self._prob_matrix is None:
            raise RuntimeError("策略未训练，请先调用 fit()")
        self._mapper.reset_weights()
        signals = self._mapper.map_states_to_weights(self._prob_matrix)
        return signals
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestHMMMacroStrategy -v`
Expected: PASS — 3 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add strategies/hmm_macro_strategy.py tests/test_strategy.py
git commit -m "feat(strategies): 新增 HMMMacroStrategy（改写自 portfolio_service）"
```

---

## Task 4: `StrategyLoader`（importlib 动态扫描）

**Files:**
- Create: `strategies/loader.py`
- Test: `tests/test_strategy.py`（追加 `TestStrategyLoader`）

**Interfaces:**
- Consumes: `BaseStrategy`（Task 1）+ 所有已注册策略（Task 2/3）
- Produces: `StrategyLoader.scan()/get(name)/list()`

- [ ] **Step 1: 写失败测试**

在 `tests/test_strategy.py` 追加（import 区补 `from strategies.loader import StrategyLoader`）：
```python
class TestStrategyLoader:
    """测试策略动态加载器"""

    def test_scan_registers_strategies(self):
        """scan 后注册了 ma_cross 与 hmm_macro"""
        loader = StrategyLoader()
        loader.scan()
        names = set(loader.list_names())
        assert "ma_cross" in names
        assert "hmm_macro" in names

    def test_get_returns_class(self):
        """get 返回策略类"""
        loader = StrategyLoader()
        loader.scan()
        cls = loader.get("ma_cross")
        assert cls.name == "ma_cross"

    def test_get_unknown_raises(self):
        """未注册策略 raise KeyError"""
        loader = StrategyLoader()
        loader.scan()
        with pytest.raises(KeyError):
            loader.get("not_exist")

    def test_list_returns_metadata(self):
        """list 返回 [{name, universe}] 供 API"""
        loader = StrategyLoader()
        loader.scan()
        items = loader.list()
        assert any(it["name"] == "ma_cross" for it in items)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py::TestStrategyLoader -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 写最小实现**

新建 `strategies/loader.py`：
```python
"""策略动态加载器（importlib）

启动时扫描 strategies/ 白名单目录下所有模块，收集带 name 的 BaseStrategy 子类。
安全红线：只扫描 strategies/ 目录（非任意路径）；要求类显式声明 name 才注册，
杜绝隐式/恶意加载。
"""
import importlib
import inspect
import pkgutil
from typing import Dict, List, Type

from .base import BaseStrategy


class StrategyLoader:
    def __init__(self, package_name: str = "strategies"):
        self._package = package_name
        self._registry: Dict[str, Type[BaseStrategy]] = {}

    def scan(self) -> None:
        """扫描策略包，注册所有带 name 的 BaseStrategy 子类"""
        pkg = importlib.import_module(self._package)
        for _, modname, _ in pkgutil.iter_modules(pkg.__path__):
            module = importlib.import_module(f"{self._package}.{modname}")
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if (issubclass(cls, BaseStrategy)
                        and cls is not BaseStrategy
                        and getattr(cls, "name", None)):
                    # 用类属性 name 注册（实例化时 universe 才注入）
                    self._registry[cls.name] = cls

    def get(self, name: str) -> Type[BaseStrategy]:
        """按 name 获取策略类"""
        if name not in self._registry:
            raise KeyError(f"未注册的策略: {name}，可用: {list(self._registry.keys())}")
        return self._registry[name]

    def list_names(self) -> List[str]:
        return list(self._registry.keys())

    def list(self) -> List[Dict[str, object]]:
        """返回策略元数据（供 /api/v1/strategies）"""
        return [
            {"name": name, "universe": getattr(cls, "universe", [])}
            for name, cls in self._registry.items()
        ]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestStrategyLoader -v`
Expected: PASS — 4 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add strategies/loader.py tests/test_strategy.py
git commit -m "feat(strategies): 新增 StrategyLoader（importlib 动态扫描）"
```

---

## Task 5: `GET /api/v1/strategies` 接口 + main.py lifespan 扫描

**Files:**
- Create: `server/api/v1/strategies.py`
- Modify: `server/main.py`（lifespan 启动扫描 + 注册路由）
- Test: `tests/test_strategy.py`（追加 API 烟测，用 FastAPI TestClient）

**Interfaces:**
- Consumes: `StrategyLoader`（Task 4）
- Produces: `GET /api/v1/strategies` → `[{name, universe}]`

- [ ] **Step 1: 写失败测试**

在 `tests/test_strategy.py` 追加（import 区补）：
```python
from fastapi.testclient import TestClient
from server.main import app
```

```python
class TestStrategiesAPI:
    """测试 /api/v1/strategies 接口"""

    def setup_method(self):
        self.client = TestClient(app)

    def test_list_strategies_endpoint(self):
        """GET /api/v1/strategies 返回策略列表"""
        resp = self.client.get("/api/v1/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        names = [it["name"] for it in data]
        assert "ma_cross" in names
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py::TestStrategiesAPI -v`
Expected: FAIL — 404 或路由未注册

- [ ] **Step 3a: 新建 `server/api/v1/strategies.py`**

```python
# -*- coding: utf-8 -*-
"""策略查询路由"""
from typing import Dict, List

from fastapi import APIRouter, Request

router = APIRouter(prefix="/strategies", tags=["策略"])


@router.get("", summary="列出已注册策略")
async def list_strategies(request: Request) -> List[Dict]:
    """返回启动时扫描注册的全部策略（供前端下拉框）"""
    loader = request.app.state.strategy_loader
    return loader.list()
```

- [ ] **Step 3b: 改 `server/main.py` 接 lifespan + 路由**

在 `server/main.py` 顶部 import 区补：
```python
from contextlib import asynccontextmanager
from strategies.loader import StrategyLoader
from server.api.v1.strategies import router as strategies_router
```

把 `app = FastAPI(...)`（第 27-34 行）改为 lifespan 形式：
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：扫描策略注册到 app.state（模块④会在同一 lifespan 追加 scheduler）
    loader = StrategyLoader()
    loader.scan()
    app.state.strategy_loader = loader
    yield
    # 销毁：模块④在此追加 scheduler.shutdown()

app = FastAPI(
    title="Quanter 量化回测平台",
    description="基于 HMM 宏观状态识别的多资产组合回测 API。",
    version="2.0.0",
    lifespan=lifespan,
)
```

在路由挂载区（第 48-49 行后）补：
```python
app.include_router(strategies_router, prefix="/api/v1")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestStrategiesAPI -v`
Expected: PASS

- [ ] **Step 5: 回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add server/api/v1/strategies.py server/main.py tests/test_strategy.py
git commit -m "feat(api): 新增 GET /api/v1/strategies + lifespan 启动扫描策略"
```

---

## Task 6: `BacktestRequest` 增 `strategy_name` + `backtest_service` 走策略

**Files:**
- Modify: `server/schemas/backtest.py`（`BacktestRequest` 增字段）
- Modify: `server/services/backtest_service.py`（`run_single_backtest` 改走策略 + run_portfolio）
- Test: `tests/test_strategy.py`（追加 service 烟测）

**Interfaces:**
- Consumes: 策略系统（Task 1-4）、`BacktestEngine.run_portfolio`
- Produces: 单资产回测统一走策略路径

**已知行为变更**（记录，非缺陷）：原 `run_single_backtest` 用 tech+macro 融合信号；新路径默认 `MaCrossStrategy`（纯技术 MACD）。如需保留 macro 融合，可后续新增融合策略（YAGNI，本计划不含）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_strategy.py` 追加：
```python
class TestSingleBacktestViaStrategy:
    """测试单资产回测走策略路径（下线 Series 路径后）"""

    def test_single_backtest_returns_response(self):
        """默认策略（ma_cross）单资产回测返回完整响应"""
        from server.services.backtest_service import run_single_backtest
        from server.schemas.backtest import BacktestRequest
        import datetime as dt

        req = BacktestRequest(
            symbol="600000.SH",
            start_date=dt.date(2023, 1, 1),
            end_date=dt.date(2023, 6, 30),
            initial_capital=1_000_000,
            strategy_name="ma_cross",
        )
        resp = run_single_backtest(req)
        assert resp.metrics.n_trades >= 0
        assert len(resp.nav_series) > 0

    def test_single_backtest_default_strategy(self):
        """strategy_name 缺省时使用默认策略，不报错"""
        from server.services.backtest_service import run_single_backtest
        from server.schemas.backtest import BacktestRequest
        import datetime as dt

        req = BacktestRequest(
            symbol="600000.SH",
            start_date=dt.date(2023, 1, 1),
            end_date=dt.date(2023, 6, 30),
        )
        resp = run_single_backtest(req)
        assert len(resp.nav_series) > 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py::TestSingleBacktestViaStrategy -v`
Expected: FAIL — `BacktestRequest` 无 `strategy_name` 字段

- [ ] **Step 3a: `server/schemas/backtest.py` 增字段**

在 `BacktestRequest`（约第 69-108 行）的 `cost_model` 字段后追加：
```python
    strategy_name: Optional[str] = Field(
        default=None,
        description="策略名（对应 /api/v1/strategies 的 name）。缺省用默认策略"
    )
```
（`Optional` 已在文件 import，确认顶部有 `from typing import ... Optional`）

- [ ] **Step 3b: 重写 `server/services/backtest_service.py` 的 `run_single_backtest`**

把现有 `run_single_backtest`（第 46-161 行）整体替换为：
```python
def run_single_backtest(req: BacktestRequest) -> BacktestResponse:
    """
    执行单资产回测（统一走策略 + run_portfolio，已下线 Series 路径）

    流程：取数 → 清洗 → 实例化策略 → fit → generate_target_weights → run_portfolio → 序列化
    """
    from strategies.loader import StrategyLoader
    from strategies.ma_cross_strategy import MaCrossStrategy

    fetcher = MockDataFetcher(seed=DATA_DEFAULTS["mock_seed"])
    start_dt = datetime.combine(req.start_date, datetime.min.time())
    end_dt = datetime.combine(req.end_date, datetime.min.time())

    df = fetcher.fetch_ohlcv(req.symbol, start_dt, end_dt, freq=req.signal_freq)
    cleaner = DataCleaner()
    df_clean = cleaner.clean_ohlcv(df, max_fill=5)

    price_data = {req.symbol: df_clean}

    # 选策略：strategy_name 指定 → loader.get；缺省 → MaCrossStrategy
    if req.strategy_name:
        loader = StrategyLoader()
        loader.scan()
        strategy_cls = loader.get(req.strategy_name)
        strategy = strategy_cls(universe=[req.symbol])
    else:
        strategy = MaCrossStrategy(universe=[req.symbol])

    strategy.fit(price_data)
    ctx = StrategyContext(
        timestamp=start_dt,
        current_weights={req.symbol: 0.0},
        cash=req.initial_capital,
        aum=req.initial_capital,
    )
    signals = strategy.generate_target_weights(price_data, ctx)

    engine = BacktestEngine(initial_capital=req.initial_capital)
    result = engine.run_portfolio(price_data=price_data, signals=signals)

    return _serialize_backtest_result(result)
```

并在 `backtest_service.py` 顶部 import 区补：
```python
from strategies.base import StrategyContext
```
保留文件中已有的 `_serialize_backtest_result` 与 `_safe_float`（不动）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestSingleBacktestViaStrategy -v`
Expected: PASS

- [ ] **Step 5: 回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿（含现有 test_backtest.py）

- [ ] **Step 6: 提交**

```bash
git add server/schemas/backtest.py server/services/backtest_service.py tests/test_strategy.py
git commit -m "feat(backtest): 单资产回测下线 Series 路径，统一走策略+run_portfolio"
```

---

## Task 7: `portfolio_service` 瘦身（HMM 逻辑迁入策略后）

**Files:**
- Modify: `server/services/portfolio_service.py`（`run_portfolio_backtest` 改用 `HMMMacroStrategy`）
- Test: `tests/test_strategy.py`（追加组合回测烟测）

**Interfaces:**
- Consumes: `HMMMacroStrategy`（Task 3）、`BacktestEngine.run_portfolio`

- [ ] **Step 1: 写失败测试**

在 `tests/test_strategy.py` 追加：
```python
class TestPortfolioBacktestViaStrategy:
    """测试组合回测走 HMMMacroStrategy"""

    def test_portfolio_backtest_returns_response(self):
        from server.services.portfolio_service import run_portfolio_backtest
        from server.schemas.portfolio import PortfolioRequest
        import datetime as dt

        req = PortfolioRequest(
            symbols=["510300.SH", "511010.SH"],
            start_date=dt.date(2022, 1, 1),
            end_date=dt.date(2023, 6, 30),
            initial_capital=1_000_000,
            n_hmm_states=3,
            buffer_threshold=0.05,
            state_weights={
                "State_0": {"510300.SH": 0.8, "511010.SH": 0.2},
                "State_1": {"510300.SH": 0.2, "511010.SH": 0.8},
                "State_2": {"510300.SH": 0.5, "511010.SH": 0.5},
            },
        )
        resp = run_portfolio_backtest(req)
        assert len(resp.nav_series) > 0
        assert len(resp.weight_series) > 0
```

- [ ] **Step 2: 运行测试确认失败/确认改造点**

Run: `python -m pytest tests/test_strategy.py::TestPortfolioBacktestViaStrategy -v`
Expected: 可能 PASS（现有实现仍工作）——本任务是重构瘦身，测试先确保不回归

- [ ] **Step 3: 改 `portfolio_service.run_portfolio_backtest`**

把现有 `run_portfolio_backtest`（第 40-137 行）整体替换为：
```python
def run_portfolio_backtest(req: PortfolioRequest) -> PortfolioResponse:
    """执行组合回测（HMM 逻辑已迁入 HMMMacroStrategy）"""
    from strategies.hmm_macro_strategy import HMMMacroStrategy
    from strategies.base import StrategyContext

    fetcher = MockDataFetcher(seed=DATA_DEFAULTS["mock_seed"])
    start_dt = datetime.combine(req.start_date, datetime.min.time())
    end_dt = datetime.combine(req.end_date, datetime.min.time())

    price_data = {
        s: fetcher.fetch_ohlcv(s, start_dt, end_dt, freq="1d")
        for s in req.symbols
    }
    macro_df = fetcher.fetch_macro("m2", start_dt, end_dt)

    strategy = HMMMacroStrategy(
        universe=req.symbols,
        n_hmm_states=req.n_hmm_states,
        state_weights=req.state_weights,
        buffer_threshold=req.buffer_threshold,
        covariance_type=PORTFOLIO_DEFAULTS["hmm_covariance_type"],
        n_iter=PORTFOLIO_DEFAULTS["hmm_n_iter"],
        random_state=PORTFOLIO_DEFAULTS["hmm_random_state"],
    )
    strategy.fit(price_data, macro_data=macro_df)
    ctx = StrategyContext(
        timestamp=start_dt, current_weights={s: 0.0 for s in req.symbols},
        cash=req.initial_capital, aum=req.initial_capital,
    )
    signals = strategy.generate_target_weights(price_data, ctx)

    engine = BacktestEngine(initial_capital=req.initial_capital)
    result = engine.run_portfolio(price_data=price_data, signals=signals)

    return _serialize_portfolio_result(result)
```

清理文件顶部不再使用的 import（`MacroRegimeHMM`、`HMMStateMapper`、`AssetWeightConfig` 等，若已不再直接引用则删除）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestPortfolioBacktestViaStrategy -v`
Expected: PASS

- [ ] **Step 5: 全量回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add server/services/portfolio_service.py tests/test_strategy.py
git commit -m "refactor(portfolio): 组合回测 HMM 逻辑迁入 HMMMacroStrategy，service 瘦身"
```

---

## 验收标准

- [ ] `strategies/` 含 base/ma_cross/hmm_macro/loader，`StrategyLoader.scan()` 注册 2 个策略
- [ ] `GET /api/v1/strategies` 返回 `[{name, universe}]`
- [ ] `backtest_service`/`portfolio_service` 不再调用 Series 路径（`run()` 的物理删除归入模块③ engine 重构，避免与本模块 test_backtest 牵连）
- [ ] `TargetWeightSignal` 允许权重和 ≤1
- [ ] `python -m pytest tests/ -v` 全绿
- [ ] 7 个独立 commit

## 后续衔接

本模块完成后，引擎产出 Order 后直接执行（`_execute_portfolio_order`）。**模块③ 风控+Broker** 将在此插入 RiskManager 拦截与 Broker 抽象。下一份 plan 为模块③。
