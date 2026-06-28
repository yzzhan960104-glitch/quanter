# 策略参数 Schema 前端驱动 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在策略插件系统上叠加「参数 schema 声明 / JSON Schema 下发 / 前端动态渲染 / 请求注入」机制，实现前端 100% 驱动回测调参；并补齐策略骨架（base/loader/三策略）与 mytt.MACD 缺口。

**Architecture:** 每个策略类用 ClassVar `params_model`（Pydantic 模型）声明可调参数（单一真相源）；后端 `model_json_schema()` 下发含 `ui` 提示的 JSON Schema；前端按 schema 动态渲染 Tabs/控件；运行时 service 用 `params_model` 校验 `strategy_params` 后显式注入策略（禁 `**kwargs`）。单资产回测统一走 `run_portfolio`（单标的组合退化）。

**Tech Stack:** Python 3、pandas、numpy、Pydantic v2、FastAPI、pytest；Vue3 Composition API + Element Plus + Axios + TypeScript。复用 `factors.fusion.TargetWeightSignal/SignalDirection/signal_fusion`、`factors.technical`、`factors.macro`、`factors.hmm_macro.MacroRegimeHMM`、`factors.fusion.HMMStateMapper`、`backtest.engine.BacktestEngine`、`backtest.cost_model.CostModel`。

## Global Constraints

（摘自 spec `2026-06-28-strategy-param-driven-design.md` 与 CLAUDE.md，逐字约束）

- 全中文注释（含 Why）；扁平反黑盒，不引入策略框架第三方库；禁 `**kwargs` 注入参数
- 策略约定"fit 后只读"；并发回测由 service 层每请求 new 一个策略 + engine 实例
- `StrategyLoader` 只扫描 `strategies/` 白名单目录，要求类显式声明 `name` 才注册
- 参数分层红线：引擎级（symbol/dates/initial_capital/signal_freq/cost_model）留请求顶层；策略级（因子周期/融合权重/HMM 标量）进 `params_model`
- 风控/回撤熔断（`max_drawdown_limit`）**不做**，留模块③
- `BacktestEngine.run()` **不物理删除**（仅停止调用，物理删除归模块③）
- 单资产默认策略 = `tech_macro_fusion`（保留原 tech+macro 融合行为）
- `tech_weights` 从 `BacktestRequest` 移除（迁入 `TechMacroFusionParams.tech_weight`）
- HMM `state_weights`/`n_hmm_states`/`buffer_threshold` 留 `PortfolioRequest` 顶层（驱动矩阵形状）；`covariance_type`/`n_iter`/`release_lag`/`max_fill_days` 进 `HmmMacroParams`；`random_state=42` 不下发（保可复现）

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `factors/fusion.py` | 放宽 `TargetWeightSignal` 权重和（允许 ≤1） | 修改 |
| `factors/mytt.py` | 补 `MACD` 指标（补模块② 缺口） | 修改 |
| `strategies/__init__.py` | 策略包 | 新建 |
| `strategies/base.py` | `BaseStrategy`（含 `params_model`）+ `StrategyContext` | 新建 |
| `strategies/ma_cross_strategy.py` | MACD 策略 + `MaCrossParams` | 新建 |
| `strategies/tech_macro_fusion_strategy.py` | tech+macro 融合策略（默认）+ `TechMacroFusionParams` | 新建 |
| `strategies/hmm_macro_strategy.py` | HMM 策略 + `HmmMacroParams` | 新建 |
| `strategies/loader.py` | `StrategyLoader`（importlib 扫描） | 新建 |
| `backtest/engine.py` | `run_portfolio` 成本走 `cost_model`（成本可调不退化） | 修改 |
| `server/api/v1/strategies.py` | `GET /strategies` + `GET /strategies/{name}/schema` | 新建 |
| `server/main.py` | lifespan 扫描策略 + 挂载路由 | 修改 |
| `server/schemas/backtest.py` | `BacktestRequest` +`strategy_name`/`strategy_params`，−`tech_weights` | 修改 |
| `server/schemas/portfolio.py` | `PortfolioRequest` +`strategy_params` | 修改 |
| `server/services/backtest_service.py` | 走策略 + 注入 params + run_portfolio | 修改 |
| `server/services/portfolio_service.py` | HMM 迁策略 + 注入 params | 修改 |
| `web/src/api/backtest.ts` | +`getStrategies`/`getStrategySchema`/`strategy_params`，−`tech_weights` | 修改 |
| `web/src/components/StrategyParamForm.vue` | JSON Schema 动态表单渲染器 | 新建 |
| `web/src/components/ParamForm.vue` | 两段式（固定引擎级 + 动态策略级） | 修改 |
| `tests/test_strategy.py` | 策略/loader/API/service 测试 | 新建 |
| `tests/test_factors.py` | TargetWeightSignal 放宽测试 | 追加 |
| `tests/test_mytt.py` | MACD 测试 | 追加 |

---

## Task 1: 放宽 `TargetWeightSignal` 权重和 + `strategies/base.py`（含 params_model）

**Files:**
- Modify: `factors/fusion.py:271-274`（`TargetWeightSignal.__post_init__` 权重和校验）
- Create: `strategies/__init__.py`、`strategies/base.py`
- Test: `tests/test_factors.py`（追加 `TestTargetWeightSignalSum`）、`tests/test_strategy.py`（新建 `TestBaseStrategy`）

**Interfaces:**
- Consumes: `factors.fusion.TargetWeightSignal`（现有）
- Produces: `strategies.base.BaseStrategy`（含 `params_model` ClassVar + `__init__(universe, params)`）、`strategies.base.StrategyContext`（供 Task 3/4/5 策略继承）

**为何先做**：单资产策略产出 `{symbol: 0.5}`（部分仓位，和 0.5），现约束要求和=1 会拒绝。统一信号语义需允许 ≤1（现金为隐含剩余）。组合策略 sum=1 仍合法，向后兼容。

- [ ] **Step 1: 写失败测试（放宽约束）**

在 `tests/test_factors.py` 末尾追加（import 区确认有 `import pandas as pd`、`import pytest`，若无则补）：

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
        sig = self._sig({"510300.SH": 0.8, "511010.SH": 0.2})
        assert sig.weights["510300.SH"] == 0.8

    def test_sum_less_than_one_valid(self):
        """部分仓位（单资产退化场景）合法"""
        sig = self._sig({"600000.SH": 0.5})
        assert sig.weights["600000.SH"] == 0.5

    def test_sum_zero_valid(self):
        sig = self._sig({"600000.SH": 0.0})
        assert sig.weights["600000.SH"] == 0.0

    def test_sum_above_one_rejected(self):
        with pytest.raises(ValueError, match="超出"):
            self._sig({"600000.SH": 1.5})

    def test_negative_weight_rejected(self):
        with pytest.raises(ValueError, match="超出"):
            self._sig({"600000.SH": -0.1})
```

- [ ] **Step 2: 写失败测试（base.py 参数注入契约）**

新建 `tests/test_strategy.py`：

```python
"""策略插件系统单元测试"""
import numpy as np
import pandas as pd
import pytest


class TestBaseStrategy:
    """测试 BaseStrategy 抽象基类的参数注入契约"""

    def test_params_injected_and_readable(self):
        """__init__ 注入 params，策略内可显式读取"""
        from strategies.base import BaseStrategy, StrategyContext
        from pydantic import BaseModel, Field

        class StubParams(BaseModel):
            period: int = Field(10, ge=1, le=100)

        class StubStrategy(BaseStrategy):
            name = "stub"
            universe = []
            params_model = StubParams

            def fit(self, price_data, macro_data=None):
                pass

            def generate_target_weights(self, price_data, ctx):
                return []

        s = StubStrategy(universe=["600000.SH"], params=StubParams(period=20))
        assert s.universe == ["600000.SH"]
        assert s.params.period == 20

    def test_strategy_context_defaults(self):
        """StrategyContext 默认值"""
        from strategies.base import StrategyContext

        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01"))
        assert ctx.current_weights == {}
        assert ctx.cash == 0.0
        assert ctx.aum == 0.0
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_factors.py::TestTargetWeightSignalSum tests/test_strategy.py::TestBaseStrategy -v`
Expected: `test_sum_less_than_one_valid`、`test_sum_zero_valid` FAIL；`TestBaseStrategy` FAIL（`ModuleNotFoundError: No module named 'strategies'`）

- [ ] **Step 4a: 放宽 `factors/fusion.py` 的 `__post_init__`**

把 `factors/fusion.py` 中（约 271-274 行）：
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

- [ ] **Step 4b: 新建 `strategies/__init__.py`**

```python
"""策略插件包

设计原则：
- 每个策略一个模块，继承 BaseStrategy
- StrategyLoader 启动时 importlib 扫描本目录自动注册
- 策略只产出 List[TargetWeightSignal]，与引擎/风控/broker 解耦
- 每个策略用 ClassVar params_model 声明可调参数（JSON Schema 真相源）
"""
```

- [ ] **Step 4c: 新建 `strategies/base.py`**

```python
"""策略抽象基类与运行时上下文

统一契约：所有策略实现 fit（训练）+ generate_target_weights（产出权重信号）。
单资产策略 = 单标的组合的退化（universe 仅 1 个标的）。

参数 schema 机制（本模块核心）：
- 每个策略类用 ClassVar params_model 声明可调参数的 Pydantic 模型（JSON Schema 真相源）
- 实例化时由 service 层注入已校验的 params 对象（显式 DI，禁 **kwargs）
- 前端经 GET /api/v1/strategies/{name}/schema 拿到 model_json_schema() 动态渲染表单
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional

import pandas as pd
from pydantic import BaseModel

from factors.fusion import TargetWeightSignal


@dataclass
class StrategyContext:
    """策略运行时只读快照（防策略误改账户状态）

    策略只能读 ctx，不能持有/修改引擎的可变账户。
    current_weights 由引擎在调用前注入（迟滞滤波/方向判定基准）。

    属性：
        timestamp: 当前信号时间戳
        current_weights: 当前实际权重 {symbol: weight}
        cash: 可用现金
        aum: 账户总市值
    """
    timestamp: pd.Timestamp
    current_weights: Dict[str, float] = field(default_factory=dict)
    cash: float = 0.0
    aum: float = 0.0


class BaseStrategy(ABC):
    """策略抽象基类

    子类必须声明 ClassVar：
        name: 策略唯一标识（StrategyLoader 注册 key、前端下拉框 value）
        label: 中文显示名（前端下拉框 label）
        universe: 标的池（实例化时注入）
        params_model: 策略可调参数的 Pydantic 模型（JSON Schema 真相源）

    约定：fit 后实例进入只读状态；并发回测每请求 new 一个实例。
    """

    name: ClassVar[str]
    label: ClassVar[str]
    universe: ClassVar[List[str]]
    params_model: ClassVar[type[BaseModel]]

    def __init__(self, universe: List[str], params: BaseModel):
        """
        显式依赖注入。

        参数：
            universe: 标的池
            params: 已由 service 层用 self.params_model 校验过的参数对象
                    （禁 **kwargs；策略内部以 self.params.<field> 显式读取）
        """
        self.universe = list(universe)
        self.params = params

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

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_factors.py::TestTargetWeightSignalSum tests/test_strategy.py::TestBaseStrategy -v`
Expected: PASS（7 个测试全绿）

- [ ] **Step 6: 回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿（放宽约束未破坏现有组合回测——它们 sum=1 仍合法）

- [ ] **Step 7: 提交**

```bash
git add factors/fusion.py strategies/__init__.py strategies/base.py tests/test_factors.py tests/test_strategy.py
git commit -m "feat(strategies): 放宽 TargetWeightSignal 权重和 + 新增 BaseStrategy(含 params_model)"
```

---

## Task 2: 在 `factors/mytt.py` 实现 `MACD`（补模块② 缺口）

**Files:**
- Modify: `factors/mytt.py`（末尾追加 `MACD`）
- Test: `tests/test_mytt.py`（追加 `TestMacd`）

**Interfaces:**
- Consumes: `factors.mytt.EMA`（现有）
- Produces: `factors.mytt.MACD(close, fast, slow, signal) -> (dif, dea, hist)`（供 Task 3 MaCrossStrategy 使用）

**为何先做**：模块② 仅落地 EMA/MA，oskhquant spec §5.2 规划的 MACD 缺失。模块① 计划的 MaCrossStrategy `from factors.mytt import MACD` 会 ImportError。先补齐。

- [ ] **Step 1: 写失败测试**

在 `tests/test_mytt.py` 末尾追加（import 区确认有 `import pandas as pd`、`import numpy as np`、`import pytest`，若无则补）：

```python
class TestMacd:
    """测试 MACD 指标（通达信约定）"""

    def _close(self, n=60, seed=42):
        np.random.seed(seed)
        return pd.Series(100 + np.cumsum(np.random.randn(n)))

    def test_returns_three_series(self):
        """返回 DIF/DEA/HIST 三个同索引序列"""
        from factors.mytt import MACD

        close = self._close()
        dif, dea, hist = MACD(close, fast=12, slow=26, signal=9)
        assert len(dif) == len(close)
        assert len(dea) == len(close)
        assert len(hist) == len(close)

    def test_dif_formula(self):
        """DIF = EMA(close,fast) - EMA(close,slow)"""
        from factors.mytt import MACD, EMA

        close = self._close()
        dif, _, _ = MACD(close, 12, 26, 9)
        expected = EMA(close, 12) - EMA(close, 26)
        pd.testing.assert_series_equal(dif, expected, check_names=False)

    def test_dea_is_ema_of_dif(self):
        """DEA = EMA(DIF, signal)——对 DIF 再求 EMA，非对 close"""
        from factors.mytt import MACD, EMA

        close = self._close()
        dif, dea, _ = MACD(close, 12, 26, 9)
        pd.testing.assert_series_equal(dea, EMA(dif, 9), check_names=False)

    def test_hist_double_difference(self):
        """HIST = (DIF - DEA) * 2（通达信约定）"""
        from factors.mytt import MACD

        close = self._close()
        dif, dea, hist = MACD(close, 12, 26, 9)
        expected = (dif - dea) * 2
        pd.testing.assert_series_equal(hist, expected, check_names=False)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_mytt.py::TestMacd -v`
Expected: FAIL — `ImportError: cannot import name 'MACD' from 'factors.mytt'`

- [ ] **Step 3: 实现 `MACD`**

在 `factors/mytt.py` 末尾追加：

```python
def MACD(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    MACD 指标（Moving Average Convergence Divergence，通达信约定）

    物理含义：快慢 EMA 之差（DIF）衡量短期与长期趋势的背离；
    对 DIF 再求 EMA 得信号线 DEA；柱状图 HIST 放大两者的差。

    通达信约定（区别于部分西方库）：
    - DEA = EMA(DIF, signal)，即对 DIF 求 EMA，而非对 close
    - HIST = (DIF - DEA) * 2

    参数：
        s: 输入序列（通常为 close）
        fast: 快线周期（默认 12）
        slow: 慢线周期（默认 26）
        signal: 信号线周期（默认 9）

    返回：
        (DIF, DEA, HIST) 三元组，均为与 s 同索引的 pd.Series

    约束说明：
        fast < slow；signal ≥ 1。前视偏差由调用方用 shift(1) 控制。
    """
    dif = EMA(s, fast) - EMA(s, slow)
    dea = EMA(dif, signal)          # 关键：对 DIF 再求 EMA，非对 close
    hist = (dif - dea) * 2
    return dif, dea, hist
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_mytt.py::TestMacd -v`
Expected: PASS（4 个测试全绿）

- [ ] **Step 5: 提交**

```bash
git add factors/mytt.py tests/test_mytt.py
git commit -m "feat(mytt): 补实现 MACD 指标（补模块② 缺口）"
```

---

## Task 3: `MaCrossStrategy`（含 `MaCrossParams`）

**Files:**
- Create: `strategies/ma_cross_strategy.py`
- Test: `tests/test_strategy.py`（追加 `TestMaCrossStrategy`）

**Interfaces:**
- Consumes: `BaseStrategy`/`StrategyContext`（Task 1）、`factors.mytt.MACD`（Task 2）
- Produces: `MaCrossStrategy`（`name="ma_cross"`、`params_model=MaCrossParams`），`__init__(universe, params=None)`

- [ ] **Step 1: 写失败测试**

在 `tests/test_strategy.py` 追加（import 区补）：

```python
from strategies.base import BaseStrategy, StrategyContext
from strategies.ma_cross_strategy import MaCrossStrategy, MaCrossParams


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
        strat = MaCrossStrategy(universe=["600000.SH"])
        assert isinstance(strat, BaseStrategy)

    def test_has_name_and_params_model(self):
        assert MaCrossStrategy.name == "ma_cross"
        assert MaCrossStrategy.params_model is MaCrossParams

    def test_default_params_valid(self):
        """默认参数合法"""
        p = MaCrossParams()
        assert p.fast == 12 and p.slow == 26 and p.signal == 9

    def test_params_out_of_range_rejected(self):
        """超范围参数被 Pydantic 拒绝"""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MaCrossParams(fast=1)       # ge=2
        with pytest.raises(ValidationError):
            MaCrossParams(slow=500)     # le=120

    def test_custom_params_take_effect(self, single_price_data):
        """自定义参数注入后生效"""
        strat = MaCrossStrategy(universe=["600000.SH"], params=MaCrossParams(fast=5, slow=20, signal=5))
        assert strat.params.fast == 5

    def test_fit_is_noop(self, single_price_data):
        strat = MaCrossStrategy(universe=["600000.SH"])
        strat.fit(single_price_data)  # 不抛异常

    def test_generate_returns_signals(self, single_price_data):
        strat = MaCrossStrategy(universe=["600000.SH"])
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(single_price_data, ctx)
        assert isinstance(signals, list)
        assert len(signals) > 0
        from factors.fusion import TargetWeightSignal
        assert all(isinstance(s, TargetWeightSignal) for s in signals)

    def test_weights_in_zero_one(self, single_price_data):
        strat = MaCrossStrategy(universe=["600000.SH"])
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(single_price_data, ctx)
        for s in signals:
            for w in s.weights.values():
                assert 0.0 <= w <= 1.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py::TestMaCrossStrategy -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'strategies.ma_cross_strategy'`

- [ ] **Step 3: 写实现**

新建 `strategies/ma_cross_strategy.py`：

```python
"""MACD 双均线策略（单资产示例，演示 BaseStrategy + params_model + MyTT 用法）

策略逻辑（与 factors/technical.py.macd 的金叉/死叉一致）：
- MACD 金叉（DIF 上穿 DEA）→ 满仓（weight=1.0）
- MACD 死叉（DIF 下穿 DEA）→ 空仓（weight=0.0）
- 持仓状态（DIF>DEA）→ 维持前值

参数全部经 MaCrossParams 声明，前端可通过 JSON Schema 下发动态调节。
"""
from typing import ClassVar, Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, Field

from factors.fusion import TargetWeightSignal, SignalDirection
from factors.mytt import MACD
from .base import BaseStrategy, StrategyContext


class MaCrossParams(BaseModel):
    """MACD 策略可调参数（JSON Schema 真相源）"""

    fast: int = Field(
        12, ge=2, le=60,
        description="MACD 快线周期（EMA）",
        json_schema_extra={"ui": {"control": "slider", "group": "MACD均线", "step": 1}},
    )
    slow: int = Field(
        26, ge=10, le=120,
        description="MACD 慢线周期（EMA）",
        json_schema_extra={"ui": {"control": "slider", "group": "MACD均线"}},
    )
    signal: int = Field(
        9, ge=3, le=30,
        description="MACD 信号线周期（对 DIF 再求 EMA）",
        json_schema_extra={"ui": {"control": "slider", "group": "MACD均线"}},
    )


class MaCrossStrategy(BaseStrategy):
    """单标的 MACD 金叉/死叉策略"""

    name: ClassVar[str] = "ma_cross"
    label: ClassVar[str] = "MACD双均线"
    params_model: ClassVar[type[BaseModel]] = MaCrossParams

    def __init__(self, universe: List[str], params: Optional[MaCrossParams] = None):
        # params 缺省用模型默认值；service 层正常会注入请求参数
        super().__init__(universe, params or MaCrossParams())
        if len(self.universe) != 1:
            raise ValueError(f"MaCrossStrategy 仅支持单标的，当前 universe: {self.universe}")
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
        dif, dea, _ = MACD(df["close"], self.params.fast, self.params.slow, self.params.signal)

        # 金叉/死叉判定（shift(1) 防前视偏差）
        golden = (dif.shift(1) < dea.shift(1)) & (dif > dea)
        death = (dif.shift(1) > dea.shift(1)) & (dif < dea)

        weight = pd.Series(0.5, index=df.index)   # 默认半仓（中性）
        weight[golden] = 1.0
        weight[death] = 0.0
        # 持仓状态：DIF>DEA 维持前值
        holding = (dif > dea) & ~golden & ~death
        weight[holding] = weight[holding].shift(1)
        weight = weight.ffill().fillna(0.0).clip(0.0, 1.0)

        # direction 设为 BUY（非 HOLD）使引擎纳入调仓评估；
        # 实际买卖由引擎按 delta 符号 + 整手过滤决定
        return [
            TargetWeightSignal(
                timestamp=ts,
                weights={self._symbol: float(w)},
                directions={self._symbol: SignalDirection.BUY},
            )
            for ts, w in weight.items()
        ]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestMaCrossStrategy -v`
Expected: PASS（8 个测试全绿）

- [ ] **Step 5: 提交**

```bash
git add strategies/ma_cross_strategy.py tests/test_strategy.py
git commit -m "feat(strategies): 新增 MaCrossStrategy（含 MaCrossParams 参数模型）"
```

---

## Task 4: `TechMacroFusionStrategy`（单资产默认策略，保留原融合行为）

**Files:**
- Create: `strategies/tech_macro_fusion_strategy.py`
- Test: `tests/test_strategy.py`（追加 `TestTechMacroFusionStrategy`）

**Interfaces:**
- Consumes: `BaseStrategy`（Task 1）、`factors.technical.moving_average_cross/volume_price_trend`、`factors.macro.macro_anchor_signal`、`factors.fusion.signal_fusion`（均现有）
- Produces: `TechMacroFusionStrategy`（`name="tech_macro_fusion"`、`params_model=TechMacroFusionParams`），搬迁自现 `backtest_service` 步骤 3-5

**为何做**：单资产默认策略，保留原 tech+macro 融合行为（消除模块① 计划"默认策略变纯 MACD"的行为变更隐患），且其参数正中用户调参诉求（MA 周期、VPT 窗口、宏观阈值、融合权重）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_strategy.py` 追加（import 区补）：

```python
from strategies.tech_macro_fusion_strategy import (
    TechMacroFusionStrategy, TechMacroFusionParams,
)


@pytest.fixture
def single_macro_data():
    """月频宏观数据（M2）"""
    return pd.DataFrame(
        {"m2": np.linspace(200, 220, 25)},
        index=pd.date_range("2023-01-01", periods=25, freq="MS", tz="Asia/Shanghai"),
    )


class TestTechMacroFusionStrategy:
    """测试 tech+macro 融合策略（单资产默认）"""

    def test_has_name_and_params_model(self):
        assert TechMacroFusionStrategy.name == "tech_macro_fusion"
        assert TechMacroFusionStrategy.params_model is TechMacroFusionParams

    def test_default_params(self):
        p = TechMacroFusionParams()
        assert p.ma_short == 5 and p.ma_long == 20
        assert p.tech_weight == 0.7

    def test_fit_stores_macro(self, single_price_data, single_macro_data):
        """fit 存储 macro_data 供 generate 使用"""
        strat = TechMacroFusionStrategy(universe=["600000.SH"])
        strat.fit(single_price_data, macro_data=single_macro_data)
        assert strat._macro_df is not None

    def test_generate_with_macro(self, single_price_data, single_macro_data):
        """有宏观数据时产出融合信号"""
        strat = TechMacroFusionStrategy(universe=["600000.SH"])
        strat.fit(single_price_data, macro_data=single_macro_data)
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(single_price_data, ctx)
        assert len(signals) > 0
        from factors.fusion import TargetWeightSignal
        assert all(isinstance(s, TargetWeightSignal) for s in signals)

    def test_generate_without_macro_falls_back_to_tech(self, single_price_data):
        """无宏观数据时退化为纯技术信号（不抛异常）"""
        strat = TechMacroFusionStrategy(universe=["600000.SH"])
        strat.fit(single_price_data, macro_data=None)
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(single_price_data, ctx)
        assert len(signals) > 0

    def test_custom_ma_periods_take_effect(self, single_price_data, single_macro_data):
        """自定义 MA 周期注入后影响信号（与默认不同）"""
        strat_default = TechMacroFusionStrategy(universe=["600000.SH"])
        strat_custom = TechMacroFusionStrategy(
            universe=["600000.SH"],
            params=TechMacroFusionParams(ma_short=3, ma_long=10),
        )
        strat_default.fit(single_price_data, macro_data=single_macro_data)
        strat_custom.fit(single_price_data, macro_data=single_macro_data)
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        s_def = strat_default.generate_target_weights(single_price_data, ctx)
        s_cust = strat_custom.generate_target_weights(single_price_data, ctx)
        # 不同 MA 周期 → 信号序列应有差异
        w_def = [s.weights["600000.SH"] for s in s_def]
        w_cust = [s.weights["600000.SH"] for s in s_cust]
        assert w_def != w_cust

    def test_weights_in_zero_one(self, single_price_data, single_macro_data):
        strat = TechMacroFusionStrategy(universe=["600000.SH"])
        strat.fit(single_price_data, macro_data=single_macro_data)
        ctx = StrategyContext(timestamp=pd.Timestamp("2023-01-01", tz="Asia/Shanghai"))
        for s in strat.generate_target_weights(single_price_data, ctx):
            for w in s.weights.values():
                assert 0.0 <= w <= 1.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py::TestTechMacroFusionStrategy -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

新建 `strategies/tech_macro_fusion_strategy.py`：

```python
"""技术+宏观融合策略（单资产默认策略）

逻辑搬迁自现 server/services/backtest_service.run_single_backtest 步骤 3-5：
MA 双均线 + VPT 等权 → 技术信号；与宏观锚点信号按 tech_weight 加权融合。
封装为策略后，service 层只负责取数 + 实例化 + run_portfolio。

参数经 TechMacroFusionParams 声明，前端可动态调节（消除原 service 层硬编码）。
"""
from typing import ClassVar, Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, Field

from factors.fusion import TargetWeightSignal, SignalDirection, signal_fusion
from factors.macro import macro_anchor_signal
from factors.technical import moving_average_cross, volume_price_trend
from .base import BaseStrategy, StrategyContext


class TechMacroFusionParams(BaseModel):
    """技术+宏观融合策略可调参数（JSON Schema 真相源）"""

    ma_short: int = Field(
        5, ge=1, le=60,
        description="短均线周期（SMA，天）",
        json_schema_extra={"ui": {"control": "slider", "group": "均线", "step": 1}},
    )
    ma_long: int = Field(
        20, ge=5, le=250,
        description="长均线周期（SMA，天）",
        json_schema_extra={"ui": {"control": "slider", "group": "均线"}},
    )
    vpt_window: int = Field(
        20, ge=5, le=120,
        description="量价趋势(VPT)窗口（天）",
        json_schema_extra={"ui": {"control": "slider", "group": "量价"}},
    )
    macro_threshold: float = Field(
        0.02, ge=0.0, le=0.2,
        description="宏观锚点阈值（M2 环比增速）",
        json_schema_extra={"ui": {"control": "slider", "group": "宏观", "step": 0.005}},
    )
    macro_window: int = Field(
        3, ge=1, le=12,
        description="宏观连续超阈值期数",
        json_schema_extra={"ui": {"control": "slider", "group": "宏观"}},
    )
    tech_weight: float = Field(
        0.7, ge=0.0, le=1.0,
        description="技术信号融合权重（宏观权重 = 1 − tech_weight）",
        json_schema_extra={"ui": {"control": "slider", "group": "融合", "step": 0.05}},
    )


class TechMacroFusionStrategy(BaseStrategy):
    """技术+宏观融合策略（单资产默认）"""

    name: ClassVar[str] = "tech_macro_fusion"
    label: ClassVar[str] = "技术+宏观融合"
    params_model: ClassVar[type[BaseModel]] = TechMacroFusionParams

    def __init__(self, universe: List[str], params: Optional[TechMacroFusionParams] = None):
        super().__init__(universe, params or TechMacroFusionParams())
        if len(self.universe) != 1:
            raise ValueError(
                f"TechMacroFusionStrategy 仅支持单标的，当前 universe: {self.universe}"
            )
        self._symbol = self.universe[0]
        self._macro_df: Optional[pd.DataFrame] = None

    def fit(
        self,
        price_data: Dict[str, pd.DataFrame],
        macro_data: Optional[pd.DataFrame] = None,
    ) -> None:
        """存储宏观数据供 generate 使用（无训练）"""
        self._macro_df = macro_data

    def generate_target_weights(
        self,
        price_data: Dict[str, pd.DataFrame],
        ctx: StrategyContext,
    ) -> List[TargetWeightSignal]:
        """MA+VPT 技术信号 与 宏观锚点信号 融合 → 目标权重信号"""
        p = self.params
        df = price_data[self._symbol]

        # 技术信号：双均线 + VPT 等权（与原 service 步骤 3 一致）
        ma_signal = moving_average_cross(df, short_window=p.ma_short, long_window=p.ma_long)
        vpt_signal = volume_price_trend(df, window=p.vpt_window)
        tech_signal = (ma_signal + vpt_signal) / 2

        # 宏观融合：有 macro_df 则融合，失败/缺失则退化为纯技术（保留原 service 容错）
        try:
            if self._macro_df is not None and not self._macro_df.empty:
                macro_signal = macro_anchor_signal(
                    self._macro_df,
                    indicator="m2",
                    threshold=p.macro_threshold,
                    window=p.macro_window,
                )
                aligned_index = tech_signal.index.intersection(macro_signal.index)
                if len(aligned_index) > 0:
                    fused = signal_fusion(
                        tech_signal.loc[aligned_index],
                        macro_signal.loc[aligned_index],
                        weights={"tech": p.tech_weight, "macro": 1.0 - p.tech_weight},
                    )
                else:
                    fused = tech_signal.clip(0.0, 1.0)
            else:
                fused = tech_signal.clip(0.0, 1.0)
        except (ValueError, KeyError):
            # 宏观信号计算失败时退化为纯技术信号（防范异常中断回测）
            fused = tech_signal.clip(0.0, 1.0)

        return [
            TargetWeightSignal(
                timestamp=ts,
                weights={self._symbol: float(w)},
                directions={self._symbol: SignalDirection.BUY},
            )
            for ts, w in fused.items()
        ]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestTechMacroFusionStrategy -v`
Expected: PASS（7 个测试全绿）

- [ ] **Step 5: 提交**

```bash
git add strategies/tech_macro_fusion_strategy.py tests/test_strategy.py
git commit -m "feat(strategies): 新增 TechMacroFusionStrategy（单资产默认，保留原融合行为）"
```

---

## Task 5: `HMMMacroStrategy`（含 `HmmMacroParams`）

**Files:**
- Create: `strategies/hmm_macro_strategy.py`
- Test: `tests/test_strategy.py`（追加 `TestHMMMacroStrategy`）

**Interfaces:**
- Consumes: `BaseStrategy`（Task 1）、`factors.hmm_macro.MacroRegimeHMM`、`factors.fusion.HMMStateMapper/AssetWeightConfig`（均现有）
- Produces: `HMMMacroStrategy`（`name="hmm_macro"`、`params_model=HmmMacroParams`），ctor 额外收结构性配置 `n_hmm_states`/`state_weights`/`buffer_threshold`

- [ ] **Step 1: 写失败测试**

在 `tests/test_strategy.py` 追加（import 区补）：

```python
from strategies.hmm_macro_strategy import HMMMacroStrategy, HmmMacroParams


@pytest.fixture
def multi_price_data():
    """双标的 OHLCV"""
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


@pytest.fixture
def multi_macro_data():
    return pd.DataFrame(
        {"m2": np.linspace(200, 220, 25)},
        index=pd.date_range("2022-01-01", periods=25, freq="MS", tz="Asia/Shanghai"),
    )


class TestHMMMacroStrategy:
    """测试 HMM 宏观策略"""

    STATE_WEIGHTS = {
        "State_0": {"510300.SH": 0.8, "511010.SH": 0.2},
        "State_1": {"510300.SH": 0.2, "511010.SH": 0.8},
        "State_2": {"510300.SH": 0.5, "511010.SH": 0.5},
    }

    def _make(self, **overrides):
        kwargs = dict(
            universe=["510300.SH", "511010.SH"],
            n_hmm_states=3,
            state_weights=self.STATE_WEIGHTS,
            buffer_threshold=0.05,
        )
        kwargs.update(overrides)
        return HMMMacroStrategy(**kwargs)

    def test_has_name_and_params_model(self):
        assert HMMMacroStrategy.name == "hmm_macro"
        assert HMMMacroStrategy.params_model is HmmMacroParams

    def test_default_params(self):
        p = HmmMacroParams()
        assert p.covariance_type == "diag"
        assert p.release_lag == 5 and p.max_fill_days == 90

    def test_fit_then_generate(self, multi_price_data, multi_macro_data):
        strat = self._make()
        strat.fit(multi_price_data, macro_data=multi_macro_data)
        ctx = StrategyContext(timestamp=pd.Timestamp("2022-01-01", tz="Asia/Shanghai"))
        signals = strat.generate_target_weights(multi_price_data, ctx)
        assert len(signals) > 0
        from factors.fusion import TargetWeightSignal
        assert all(isinstance(s, TargetWeightSignal) for s in signals)

    def test_signals_cover_universe(self, multi_price_data, multi_macro_data):
        strat = self._make()
        strat.fit(multi_price_data, macro_data=multi_macro_data)
        ctx = StrategyContext(timestamp=pd.Timestamp("2022-01-01", tz="Asia/Shanghai"))
        for s in strat.generate_target_weights(multi_price_data, ctx):
            assert set(s.weights.keys()) == {"510300.SH", "511010.SH"}

    def test_custom_release_lag_used(self, multi_price_data, multi_macro_data):
        """自定义 release_lag 注入 HMM 对齐"""
        strat = self._make(params=HmmMacroParams(release_lag=10, max_fill_days=120))
        assert strat.params.release_lag == 10
        strat.fit(multi_price_data, macro_data=multi_macro_data)
        # 不抛异常即表明对齐用了自定义参数

    def test_fit_without_macro_raises(self, multi_price_data):
        """HMM 策略必须有宏观数据"""
        strat = self._make()
        with pytest.raises(ValueError, match="宏观数据"):
            strat.fit(multi_price_data, macro_data=None)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py::TestHMMMacroStrategy -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

新建 `strategies/hmm_macro_strategy.py`：

```python
"""HMM 宏观状态识别 + 迟滞调仓策略

逻辑搬迁自现 server/services/portfolio_service.run_portfolio_backtest 步骤 2-5：
对齐宏观数据 → 训练 HMM → 预测状态概率 → mapper 映射为目标权重信号。

参数分层：
- HmmMacroParams（策略级，schema 下发）：covariance_type / n_iter / release_lag / max_fill_days
- 结构性配置（请求级 ctor 直传，不进 schema）：n_hmm_states / state_weights / buffer_threshold
  原因：state_weights 是 State_N × symbols 矩阵，行列依赖 n_hmm_states 与 universe，无法静态 schema 化。
"""
from typing import ClassVar, Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, Field

from factors.fusion import (
    HMMStateMapper, AssetWeightConfig, TargetWeightSignal,
)
from factors.hmm_macro import MacroRegimeHMM
from .base import BaseStrategy, StrategyContext


class HmmMacroParams(BaseModel):
    """HMM 宏观策略可调参数（JSON Schema 真相源，仅标量训练参数）"""

    covariance_type: str = Field(
        "diag",
        description="HMM 协方差类型（diag 稳定 / full 灵活易过拟合 / tied / spherical）",
        json_schema_extra={"ui": {
            "control": "select", "group": "HMM训练",
            "options": [
                {"label": "对角(diag)", "value": "diag"},
                {"label": "完全(full)", "value": "full"},
                {"label": "绑定(tied)", "value": "tied"},
                {"label": "球面(spherical)", "value": "spherical"},
            ],
        }},
    )
    n_iter: int = Field(
        100, ge=10, le=500,
        description="EM 算法最大迭代次数",
        json_schema_extra={"ui": {"control": "input-number", "group": "HMM训练"}},
    )
    release_lag: int = Field(
        5, ge=0, le=30,
        description="宏观数据发布滞后（天，防未来函数）",
        json_schema_extra={"ui": {"control": "slider", "group": "数据对齐"}},
    )
    max_fill_days: int = Field(
        90, ge=10, le=365,
        description="宏观前向填充最大天数（超此标记 NaN）",
        json_schema_extra={"ui": {"control": "slider", "group": "数据对齐"}},
    )


class HMMMacroStrategy(BaseStrategy):
    """HMM 宏观状态 → ETF 权重（含迟滞滤波）"""

    name: ClassVar[str] = "hmm_macro"
    label: ClassVar[str] = "HMM宏观状态"
    params_model: ClassVar[type[BaseModel]] = HmmMacroParams

    def __init__(
        self,
        universe: List[str],
        params: Optional[HmmMacroParams] = None,
        n_hmm_states: int = 3,
        state_weights: Optional[Dict[str, Dict[str, float]] = None,
        buffer_threshold: float = 0.05,
    ):
        """
        参数：
            universe: 标的池
            params: HmmMacroParams（策略级训练参数，schema 下发）
            n_hmm_states: HMM 状态数（结构性，请求级，驱动 state_weights 矩阵行数）
            state_weights: 各状态基准权重矩阵（结构性，请求级）
            buffer_threshold: 迟滞阈值（结构性，请求级）
        """
        super().__init__(universe, params or HmmMacroParams())
        self._n_states = n_hmm_states
        self._state_weights = state_weights or {}
        self._buffer = buffer_threshold

        # HMM 模型：n_components/n_states 由结构配置定，训练参数取自 params
        # random_state 不下发（=42 保可复现），由服务层统一管控
        self._hmm = MacroRegimeHMM(
            n_components=n_hmm_states,
            covariance_type=self.params.covariance_type,
            n_iter=self.params.n_iter,
            random_state=42,
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

        # 对齐宏观数据（严格防未来函数）；release_lag/max_fill_days 取自 params（消除原硬编码）
        aligned = self._hmm.align_macro_data(
            daily_df.dropna(),
            macro_data,
            release_lag=self.params.release_lag,
            max_fill_days=self.params.max_fill_days,
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
        return self._mapper.map_states_to_weights(self._prob_matrix)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestHMMMacroStrategy -v`
Expected: PASS（6 个测试全绿）

- [ ] **Step 5: 提交**

```bash
git add strategies/hmm_macro_strategy.py tests/test_strategy.py
git commit -m "feat(strategies): 新增 HMMMacroStrategy（含 HmmMacroParams，改写自 portfolio_service）"
```

---

## Task 6: `engine.run_portfolio` 成本走 `cost_model`（成本可调不退化）

**Files:**
- Modify: `backtest/engine.py:846-859`（`_execute_portfolio_order` 的成本计算段）
- Test: `tests/test_strategy.py`（追加 `TestPortfolioCostModel`）

**Interfaces:**
- Consumes: `backtest.cost_model.CostModel`（现有，engine `__init__` 已存 `self.cost_model`）
- Produces: `_execute_portfolio_order` 使用 `self.cost_model` 计算佣金/印花税/过户费

**为何做**：单资产回测统一走 `run_portfolio` 后，若 `_execute_portfolio_order` 仍硬编码成本（万三/千五/十万一），则请求传入的 `cost_model` 参数失效——违背"前端传什么引擎用什么"。本任务让现有 DI 的 `cost_model` 真正生效。

**已知边界（不做）**：滑点（slippage）需要逐标的逐日成交量数据，组合路径暂不接入，留模块③ BacktestBroker 完整 CostModel（含滑点）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_strategy.py` 追加（import 区补）：

```python
from backtest.engine import BacktestEngine
from backtest.cost_model import CostModel
from factors.fusion import TargetWeightSignal, SignalDirection


class TestPortfolioCostModel:
    """测试 run_portfolio 路径使用注入的 cost_model（成本可调不退化）"""

    def _run_with_commission(self, commission_rate):
        """用指定佣金率跑一次极简组合回测，返回总成本"""
        dates = pd.date_range("2023-01-01", periods=5, freq="D")
        df = pd.DataFrame({
            "open": [10.0] * 5, "high": [10.5] * 5, "low": [9.5] * 5,
            "close": [10.0] * 5, "volume": [1e6] * 5,
        }, index=dates)
        price_data = {"510300.SH": df}

        # 第一日满仓买入 510300
        signals = [TargetWeightSignal(
            timestamp=dates[0],
            weights={"510300.SH": 1.0},
            directions={"510300.SH": SignalDirection.BUY},
        )]

        engine = BacktestEngine(
            initial_capital=1_000_000,
            cost_model=CostModel(commission_rate=commission_rate, min_commission=0.0),
        )
        result = engine.run_portfolio(price_data=price_data, signals=signals)

        trades_df = result["trades"]
        # 买入交易的成本列之和
        return float(trades_df["cost"].sum()) if len(trades_df) > 0 else 0.0

    def test_higher_commission_yields_higher_cost(self):
        """更高佣金率 → 更高交易成本（证明 cost_model 生效）"""
        cost_low = self._run_with_commission(commission_rate=0.0001)
        cost_high = self._run_with_commission(commission_rate=0.01)
        assert cost_high > cost_low
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py::TestPortfolioCostModel -v`
Expected: FAIL — 高低佣金率成本相近或相等（现硬编码万三，cost_model 未生效）

- [ ] **Step 3: 改 `_execute_portfolio_order` 成本段**

把 `backtest/engine.py` 中 `_execute_portfolio_order`（约 846-859 行）的成本计算段：
```python
        # 计算成交金额
        amount = order.shares * order.price

        # 计算佣金
        commission = max(amount * 0.0003, 5.0)

        # 计算印花税（仅卖出）
        stamp_duty = amount * 0.0005 if order.side == OrderSide.SELL else 0.0

        # 计算过户费（仅上海市场，代码以 5 或 6 开头）
        transfer_fee = amount * 0.00001 if order.symbol.startswith(("5", "6")) else 0.0

        # 总交易成本
        total_cost = commission + stamp_duty + transfer_fee
```
改为（使用注入的 `self.cost_model`，保留与原默认值一致的物理含义）：
```python
        # 计算成交金额
        amount = order.shares * order.price

        # 成本走注入的 cost_model（使请求传入的成本参数真正生效，消除原硬编码）
        # 注意：滑点需逐标的逐日成交量，组合路径暂不接入，留模块③ BacktestBroker
        commission = self.cost_model.calculate_commission(amount)
        stamp_duty = self.cost_model.calculate_stamp_duty(amount, order.side == OrderSide.SELL)
        transfer_fee = self.cost_model.calculate_transfer_fee(amount, order.symbol)

        # 总交易成本
        total_cost = commission + stamp_duty + transfer_fee
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestPortfolioCostModel -v`
Expected: PASS

- [ ] **Step 5: 回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿（默认 cost_model 行为与原硬编码一致：佣金万三最低5元、印花千五卖出、过户十万一沪市）

- [ ] **Step 6: 提交**

```bash
git add backtest/engine.py tests/test_strategy.py
git commit -m "fix(engine): run_portfolio 成本走 cost_model，使请求成本参数生效"
```

---

## Task 7: `StrategyLoader` + `/api/v1/strategies`（列表 + schema）+ main.py lifespan

**Files:**
- Create: `strategies/loader.py`、`server/api/v1/strategies.py`
- Modify: `server/main.py`
- Test: `tests/test_strategy.py`（追加 `TestStrategyLoader`、`TestStrategiesAPI`）

**Interfaces:**
- Consumes: 所有策略类（Task 3/4/5）
- Produces: `StrategyLoader.scan/get/list`；`GET /api/v1/strategies` → `[{name, label, universe}]`；`GET /api/v1/strategies/{name}/schema` → JSON Schema

- [ ] **Step 1: 写失败测试**

在 `tests/test_strategy.py` 追加（import 区补）：

```python
from strategies.loader import StrategyLoader


class TestStrategyLoader:
    """测试策略动态加载器"""

    def test_scan_registers_strategies(self):
        loader = StrategyLoader()
        loader.scan()
        names = set(loader.list_names())
        assert "ma_cross" in names
        assert "tech_macro_fusion" in names
        assert "hmm_macro" in names

    def test_get_returns_class(self):
        loader = StrategyLoader()
        loader.scan()
        cls = loader.get("ma_cross")
        assert cls.name == "ma_cross"

    def test_get_unknown_raises(self):
        loader = StrategyLoader()
        loader.scan()
        with pytest.raises(KeyError):
            loader.get("not_exist")

    def test_list_returns_metadata_with_label(self):
        loader = StrategyLoader()
        loader.scan()
        items = loader.list()
        macross = next(it for it in items if it["name"] == "ma_cross")
        assert "label" in macross
        assert "universe" in macross
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py::TestStrategyLoader -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'strategies.loader'`

- [ ] **Step 3: 写 `strategies/loader.py`**

```python
"""策略动态加载器（importlib）

启动时扫描 strategies/ 白名单目录下所有模块，收集带 name 的 BaseStrategy 子类。
安全红线：只扫描 strategies/ 目录（非任意路径）；要求类显式声明 name 才注册，
杜绝隐式/恶意加载。

参数 schema 下发：list() 返回策略元数据；get_schema(name) 返回 params_model 的
JSON Schema（含 ui 渲染提示），供前端动态渲染表单。
"""
import importlib
import inspect
import pkgutil
from typing import Dict, List, Type, Any

from pydantic import BaseModel

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
                    # 用类属性 name 注册（universe/params 实例化时注入）
                    self._registry[cls.name] = cls

    def get(self, name: str) -> Type[BaseStrategy]:
        """按 name 获取策略类"""
        if name not in self._registry:
            raise KeyError(f"未注册的策略: {name}，可用: {list(self._registry.keys())}")
        return self._registry[name]

    def list_names(self) -> List[str]:
        return list(self._registry.keys())

    def list(self) -> List[Dict[str, Any]]:
        """返回策略元数据（供 GET /api/v1/strategies）"""
        return [
            {
                "name": name,
                "label": getattr(cls, "label", name),
                "universe": getattr(cls, "universe", []),
            }
            for name, cls in self._registry.items()
        ]

    def get_schema(self, name: str) -> Dict[str, Any]:
        """返回策略 params_model 的 JSON Schema（供 GET /strategies/{name}/schema）"""
        cls = self.get(name)
        params_model: Type[BaseModel] = cls.params_model
        return params_model.model_json_schema()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestStrategyLoader -v`
Expected: PASS（4 个测试全绿）

- [ ] **Step 5: 写失败测试（API 端点）**

在 `tests/test_strategy.py` 追加（import 区补）：

```python
from fastapi.testclient import TestClient
from server.main import app


class TestStrategiesAPI:
    """测试 /api/v1/strategies 接口"""

    def setup_method(self):
        self.client = TestClient(app)

    def test_list_strategies(self):
        resp = self.client.get("/api/v1/strategies")
        assert resp.status_code == 200
        data = resp.json()
        names = [it["name"] for it in data]
        assert "ma_cross" in names
        assert "tech_macro_fusion" in names

    def test_get_schema_returns_json_schema(self):
        """schema 端点返回含 ui 提示的 JSON Schema"""
        resp = self.client.get("/api/v1/strategies/ma_cross/schema")
        assert resp.status_code == 200
        schema = resp.json()
        assert schema["type"] == "object"
        assert "fast" in schema["properties"]
        # ui 渲染提示经 json_schema_extra 合并进字段 schema
        assert schema["properties"]["fast"].get("ui", {}).get("control") == "slider"

    def test_get_schema_unknown_strategy(self):
        resp = self.client.get("/api/v1/strategies/not_exist/schema")
        assert resp.status_code in (400, 404, 500)
```

- [ ] **Step 6: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py::TestStrategiesAPI -v`
Expected: FAIL — 404（路由未注册）

- [ ] **Step 7: 写 `server/api/v1/strategies.py`**

```python
# -*- coding: utf-8 -*-
"""策略查询路由

职责：
1. GET /api/v1/strategies —— 列出已注册策略（供前端下拉框）
2. GET /api/v1/strategies/{name}/schema —— 返回策略参数 JSON Schema（供前端动态渲染表单）

设计原则：
- 路由层只读取 app.state.strategy_loader（启动时扫描注册），不重复扫描
- schema 来自 params_model.model_json_schema()，单一真相源
"""
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/strategies", tags=["策略"])


def _get_loader(request: Request):
    """从 app.state 取启动时扫描的 StrategyLoader 单例"""
    loader = getattr(request.app.state, "strategy_loader", None)
    if loader is None:
        raise HTTPException(status_code=500, detail="策略加载器未初始化")
    return loader


@router.get("", summary="列出已注册策略")
async def list_strategies(request: Request) -> List[Dict[str, Any]]:
    """返回启动时扫描注册的全部策略（供前端下拉框）"""
    return _get_loader(request).list()


@router.get("/{name}/schema", summary="获取策略参数 JSON Schema")
async def get_strategy_schema(name: str, request: Request) -> Dict[str, Any]:
    """返回策略 params_model 的 JSON Schema（含 ui 渲染提示）"""
    loader = _get_loader(request)
    try:
        return loader.get_schema(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
```

- [ ] **Step 8: 改 `server/main.py`（lifespan + 挂载路由）**

把 `server/main.py` 顶部 import 区（第 19-24 行后）补：
```python
from contextlib import asynccontextmanager

from strategies.loader import StrategyLoader
from server.api.v1.strategies import router as strategies_router
```

把 `app = FastAPI(...)`（第 27-34 行）替换为 lifespan 形式：
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
    description=(
        "基于 HMM 宏观状态识别的多资产组合回测 API。"
        "支持单资产信号回测和多资产组合调仓回测两种模式。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)
```

在路由挂载区（第 48-49 行后）补：
```python
app.include_router(strategies_router, prefix="/api/v1")
```

并把 `/health` 端点的 `"version": "1.0.0"` 改为 `"version": "2.0.0"`（与 app 版本对齐）。

- [ ] **Step 9: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestStrategiesAPI -v`
Expected: PASS（3 个测试全绿）

- [ ] **Step 10: 回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿

- [ ] **Step 11: 提交**

```bash
git add strategies/loader.py server/api/v1/strategies.py server/main.py tests/test_strategy.py
git commit -m "feat(api): 新增 StrategyLoader + GET /strategies + /strategies/{name}/schema + lifespan"
```

---

## Task 8: `BacktestRequest`/`PortfolioRequest` 增 `strategy_params` + 删 `tech_weights` + 两 service 注入

**Files:**
- Modify: `server/schemas/backtest.py`（`BacktestRequest`）
- Modify: `server/schemas/portfolio.py`（`PortfolioRequest`）
- Modify: `server/services/backtest_service.py`（`run_single_backtest`）
- Modify: `server/services/portfolio_service.py`（`run_portfolio_backtest`）
- Test: `tests/test_strategy.py`（追加 `TestSingleBacktestViaStrategy`、`TestPortfolioBacktestViaStrategy`）

**Interfaces:**
- Consumes: 策略系统（Task 3/4/5/7）、`BacktestEngine.run_portfolio`
- Produces: 单资产/组合回测统一走策略路径，参数经 `strategy_params` 校验注入

- [ ] **Step 1: 写失败测试**

在 `tests/test_strategy.py` 追加：

```python
class TestSingleBacktestViaStrategy:
    """测试单资产回测走策略路径 + 参数注入"""

    def _req(self, **overrides):
        from server.schemas.backtest import BacktestRequest
        import datetime as dt
        kwargs = dict(
            symbol="600000.SH",
            start_date=dt.date(2023, 1, 1),
            end_date=dt.date(2023, 6, 30),
            initial_capital=1_000_000,
        )
        kwargs.update(overrides)
        return BacktestRequest(**kwargs)

    def test_default_strategy_runs(self):
        """strategy_name 缺省时用默认策略（tech_macro_fusion）"""
        from server.services.backtest_service import run_single_backtest
        resp = run_single_backtest(self._req())
        assert len(resp.nav_series) > 0
        assert resp.metrics.n_trades >= 0

    def test_explicit_strategy_runs(self):
        from server.services.backtest_service import run_single_backtest
        resp = run_single_backtest(self._req(strategy_name="ma_cross"))
        assert len(resp.nav_series) > 0

    def test_strategy_params_injected(self):
        """自定义 strategy_params 经校验后注入策略"""
        from server.services.backtest_service import run_single_backtest
        resp = run_single_backtest(self._req(
            strategy_name="tech_macro_fusion",
            strategy_params={"ma_short": 3, "ma_long": 10, "tech_weight": 0.5},
        ))
        assert len(resp.nav_series) > 0

    def test_invalid_strategy_params_rejected(self):
        """非法 strategy_params（超范围）被拒绝"""
        from server.services.backtest_service import run_single_backtest
        from server.schemas.backtest import BacktestRequest
        import datetime as dt
        # Pydantic 在 service 层 params_model(**...) 校验，超范围抛 ValueError
        req = BacktestRequest(
            symbol="600000.SH",
            start_date=dt.date(2023, 1, 1),
            end_date=dt.date(2023, 6, 30),
            strategy_name="ma_cross",
            strategy_params={"fast": 1},   # ge=2，非法
        )
        with pytest.raises(Exception):
            run_single_backtest(req)


class TestPortfolioBacktestViaStrategy:
    """测试组合回测走 HMMMacroStrategy + 标量参数注入"""

    def _req(self, **overrides):
        from server.schemas.portfolio import PortfolioRequest
        import datetime as dt
        kwargs = dict(
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
        kwargs.update(overrides)
        return PortfolioRequest(**kwargs)

    def test_portfolio_runs(self):
        from server.services.portfolio_service import run_portfolio_backtest
        resp = run_portfolio_backtest(self._req())
        assert len(resp.nav_series) > 0
        assert len(resp.weight_series) > 0

    def test_hmm_params_injected(self):
        """自定义 HMM 标量参数注入"""
        from server.services.portfolio_service import run_portfolio_backtest
        resp = run_portfolio_backtest(self._req(
            strategy_params={"covariance_type": "diag", "n_iter": 50, "release_lag": 3},
        ))
        assert len(resp.nav_series) > 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_strategy.py::TestSingleBacktestViaStrategy tests/test_strategy.py::TestPortfolioBacktestViaStrategy -v`
Expected: FAIL — `BacktestRequest` 无 `strategy_params` 字段（且仍有 `tech_weights`）

- [ ] **Step 3a: 改 `server/schemas/backtest.py`**

在 `BacktestRequest`（约第 69-137 行）：
- 删除 `tech_weights` 字段（约第 101-104 行）：
```python
    tech_weights: Dict[str, float] = Field(
        default={"tech": 0.7, "macro": 0.3},
        description="信号融合权重（和必须为 1）"
    )
```
- 删除 `validate_tech_weights` 校验器（约第 119-130 行整段 `@field_validator("tech_weights") ...`）
- 在 `cost_model` 字段后追加：
```python
    strategy_name: Optional[str] = Field(
        default=None,
        description="策略名（对应 /api/v1/strategies 的 name）。缺省用默认策略 tech_macro_fusion"
    )
    strategy_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="策略参数（键值对，由对应策略的 params_model 校验）"
    )
```
- 顶部 import 区确认有 `Any`：把 `from typing import Dict, List, Optional` 改为 `from typing import Any, Dict, List, Optional`

- [ ] **Step 3b: 改 `server/schemas/portfolio.py`**

在 `PortfolioRequest` 的 `state_weights` 字段后追加：
```python
    strategy_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="HMM 策略标量参数（covariance_type/n_iter/release_lag/max_fill_days），由 HmmMacroParams 校验"
    )
```
顶部 import 区把 `from typing import Dict, List` 改为 `from typing import Any, Dict, List`，并补 `from pydantic import ... Field`（若缺）。

- [ ] **Step 3c: 重写 `server/services/backtest_service.py` 的 `run_single_backtest`**

把 `run_single_backtest`（约第 46-161 行）整体替换为：
```python
def run_single_backtest(req: BacktestRequest) -> BacktestResponse:
    """
    执行单资产回测（统一走策略 + run_portfolio，参数经 strategy_params 校验注入）

    流程：取数 → 清洗 → 选策略 → 校验注入 params → fit → generate_target_weights
          → run_portfolio → 序列化

    全局状态污染防御：strategy 与 engine 每请求全新实例化，绝不跨请求复用。
    """
    from strategies.loader import StrategyLoader
    from strategies.base import StrategyContext

    DEFAULT_STRATEGY = "tech_macro_fusion"

    # ============ 步骤 1：取数 + 清洗 ============
    fetcher = MockDataFetcher(seed=DATA_DEFAULTS["mock_seed"])
    start_dt = datetime.combine(req.start_date, datetime.min.time())
    end_dt = datetime.combine(req.end_date, datetime.min.time())

    df = fetcher.fetch_ohlcv(req.symbol, start_dt, end_dt, freq=req.signal_freq)
    cleaner = DataCleaner()
    df_clean = cleaner.clean_ohlcv(df, max_fill=5)
    price_data = {req.symbol: df_clean}

    macro_df = fetcher.fetch_macro("m2", start_dt, end_dt)

    # ============ 步骤 2：选策略 + 校验注入参数 ============
    name = req.strategy_name or DEFAULT_STRATEGY
    loader = StrategyLoader()
    loader.scan()
    strategy_cls = loader.get(name)

    # 用策略的 params_model 校验请求参数（Pydantic 自动类型/范围校验）
    # 缺省 strategy_params → 用 params_model 默认值
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
    # 成本模型注入引擎（Task 6 使其在 run_portfolio 路径生效）
    cost_model = _build_cost_model(req.cost_model)
    engine = BacktestEngine(initial_capital=req.initial_capital, cost_model=cost_model)
    result = engine.run_portfolio(price_data=price_data, signals=signals)

    # ============ 步骤 5：序列化 ============
    return _serialize_backtest_result(result)


def _build_cost_model(cost_params):
    """从请求的 CostModelParams 构造 CostModel（缺省用默认）"""
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
```
保留文件中已有的 `_serialize_backtest_result` 与 `_safe_float`（不动）。清理不再使用的 import（`moving_average_cross`/`volume_price_trend`/`macro_anchor_signal`/`signal_fusion` 若已无直接引用则删除；`CostModel` 保留；新增无需额外 import，loader/base 在函数内 import）。

- [ ] **Step 3d: 重写 `server/services/portfolio_service.py` 的 `run_portfolio_backtest`**

把 `run_portfolio_backtest`（约第 40-137 行）整体替换为：
```python
def run_portfolio_backtest(req: PortfolioRequest) -> PortfolioResponse:
    """
    执行组合回测（HMM 逻辑已迁入 HMMMacroStrategy，标量参数经 strategy_params 注入）

    流程：取数 → 实例化 HMMMacroStrategy（注入 HmmMacroParams + 结构配置）
          → fit → generate_target_weights → run_portfolio → 序列化
    """
    from strategies.hmm_macro_strategy import HMMMacroStrategy, HmmMacroParams
    from strategies.base import StrategyContext

    # ============ 步骤 1：取数 ============
    fetcher = MockDataFetcher(seed=DATA_DEFAULTS["mock_seed"])
    start_dt = datetime.combine(req.start_date, datetime.min.time())
    end_dt = datetime.combine(req.end_date, datetime.min.time())

    price_data = {
        s: fetcher.fetch_ohlcv(s, start_dt, end_dt, freq="1d")
        for s in req.symbols
    }
    macro_df = fetcher.fetch_macro("m2", start_dt, end_dt)

    # ============ 步骤 2：校验注入 HMM 标量参数 ============
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

    # ============ 步骤 5：序列化 ============
    return _serialize_portfolio_result(result)
```
清理不再使用的 import（`MacroRegimeHMM`/`HMMStateMapper`/`AssetWeightConfig`/`SignalDirection` 若无直接引用则删除）。`PORTFOLIO_DEFAULTS` 若不再被引用则从 import 删除（`DATA_DEFAULTS` 保留）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_strategy.py::TestSingleBacktestViaStrategy tests/test_strategy.py::TestPortfolioBacktestViaStrategy -v`
Expected: PASS（6 个测试全绿）

- [ ] **Step 5: 全量回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿（含 `tests/test_backtest.py`——`engine.run()` 未删除仍可测）

- [ ] **Step 6: 提交**

```bash
git add server/schemas/backtest.py server/schemas/portfolio.py server/services/backtest_service.py server/services/portfolio_service.py tests/test_strategy.py
git commit -m "feat(backtest): 请求增 strategy_params、删 tech_weights；两 service 注入策略参数"
```

---

## Task 9: 前端动态渲染器 + ParamForm 两段式 + api.ts + 全量回归

**Files:**
- Modify: `web/src/api/backtest.ts`
- Create: `web/src/components/StrategyParamForm.vue`
- Modify: `web/src/components/ParamForm.vue`

**Interfaces:**
- Consumes: `GET /api/v1/strategies`、`GET /api/v1/strategies/{name}/schema`（Task 7）
- Produces: 前端按策略 schema 动态渲染参数表单；提交时组装 `strategy_name` + `strategy_params`

**测试说明**：项目前端无单元测试框架（`package.json` 无 vitest/jest）。本任务以 `npm run build`（vue-tsc 类型检查 + vite 构建）通过为验收门，辅以交互验证清单。

- [ ] **Step 1: 改 `web/src/api/backtest.ts`**

在类型定义区，把 `SingleBacktestParams` 改为（删除 `tech_weights`，增 strategy 字段）：
```typescript
/** 单资产回测请求 */
export interface SingleBacktestParams {
  symbol: string
  start_date: string          // YYYY-MM-DD
  end_date: string
  initial_capital: number
  signal_freq: '1d' | '1h' | '5m' | '1m'
  cost_model?: CostModelParams
  strategy_name?: string
  strategy_params?: Record<string, unknown>
}
```
`PortfolioParams` 末尾增：
```typescript
  strategy_params?: Record<string, unknown>
```
新增策略元数据/Schema 类型与 API 函数（在文件末尾的 API 函数区追加）：
```typescript
/** 策略元数据（GET /strategies 返回项） */
export interface StrategyMeta {
  name: string
  label: string
  universe: string[]
}

/** JSON Schema 字段（含 ui 渲染提示） */
export interface JsonSchemaProperty {
  type?: string
  description?: string
  minimum?: number
  maximum?: number
  default?: unknown
  enum?: string[]
  ui?: {
    control?: 'slider' | 'input-number' | 'select'
    group?: string
    step?: number
    options?: Array<{ label: string; value: string }>
  }
}

/** 策略参数 JSON Schema */
export interface StrategyParamSchema {
  type: string
  properties: Record<string, JsonSchemaProperty>
  order?: string[]
}

/** 列出已注册策略 */
export function getStrategies(): Promise<StrategyMeta[]> {
  return apiClient.get('/api/v1/strategies')
}

/** 获取策略参数 JSON Schema */
export function getStrategySchema(name: string): Promise<StrategyParamSchema> {
  return apiClient.get(`/api/v1/strategies/${name}/schema`)
}
```

- [ ] **Step 2: 新建 `web/src/components/StrategyParamForm.vue`**

```vue
<!--
  策略参数动态表单（JSON Schema 驱动）

  职责：
  1. 按 strategy_name 拉取 params_model 的 JSON Schema
  2. 按 schema.properties[*].ui.group 分组为 el-tabs
  3. 按 ui.control 渲染 slider/input-number/select（约束取自 schema，与后端同源）
  4. v-model 双向绑定到 strategyParams（提交时作为请求 strategy_params）

  设计原则：
  - 单一真相源：控件约束（min/max/step/enum）全部取自 schema，前端不重复定义
  - 0-1 浮点字段（如 tech_weight）slider 以百分比展示，回传时 ÷100
-->
<template>
  <div v-if="loading" class="spf-loading">加载策略参数…</div>
  <div v-else-if="!schema || Object.keys(schema.properties).length === 0" class="spf-empty">
    该策略无可调参数
  </div>
  <el-tabs v-else v-model="activeTab" type="border-card">
    <el-tab-pane
      v-for="group in groupedFields"
      :key="group.name"
      :label="group.name"
      :name="group.name"
    >
      <el-form label-position="top">
        <el-form-item
          v-for="key in group.fields"
          :key="key"
          :label="schema.properties[key].description || key"
        >
          <!-- slider -->
          <el-slider
            v-if="getUi(key).control === 'slider'"
            :model-value="toSlider(key)"
            :min="toSliderMin(key)"
            :max="toSliderMax(key)"
            :step="getUi(key).step ?? 1"
            :show-tooltip="true"
            style="width: 100%"
            @update:model-value="(v: number) => fromSlider(key, v)"
          />
          <!-- select -->
          <el-select
            v-else-if="getUi(key).control === 'select'"
            :model-value="strategyParams[key] as string"
            style="width: 100%"
            @update:model-value="(v: string) => setField(key, v)"
          >
            <el-option
              v-for="opt in selectOptions(key)"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
          <!-- input-number（默认） -->
          <el-input-number
            v-else
            :model-value="strategyParams[key] as number"
            :min="schema.properties[key].minimum"
            :max="schema.properties[key].maximum"
            :step="getUi(key).step ?? 1"
            style="width: 100%"
            @update:model-value="(v: number) => setField(key, v)"
          />
        </el-form-item>
      </el-form>
    </el-tab-pane>
  </el-tabs>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import type { StrategyParamSchema } from '../api/backtest'
import { getStrategySchema } from '../api/backtest'

const props = defineProps<{ strategyName: string }>()
const emit = defineEmits<{ update: [params: Record<string, unknown>] }>()

/** 当前策略参数 JSON Schema */
const schema = ref<StrategyParamSchema | null>(null)
const loading = ref(false)
/** 响应式参数值（提交时整体回传） */
const strategyParams = reactive<Record<string, unknown>>({})
const activeTab = ref('')

/** 拉取 schema 并用默认值初始化 strategyParams */
async function loadSchema(name: string) {
  loading.value = true
  try {
    const s = await getStrategySchema(name)
    schema.value = s
    // 用 schema 默认值初始化（确保缺省提交也有合法值）
    for (const [k, v] of Object.entries(s.properties)) {
      if (v.default !== undefined) strategyParams[k] = v.default
    }
    // 默认激活第一个分组
    const groups = groupedFields.value
    if (groups.length > 0) activeTab.value = groups[0].name
    emit('update', { ...strategyParams })
  } finally {
    loading.value = false
  }
}

watch(() => props.strategyName, (name) => {
  if (name) {
    // 清空旧参数（切换策略时避免残留字段污染）
    Object.keys(strategyParams).forEach((k) => delete strategyParams[k])
    loadSchema(name)
  }
}, { immediate: true })

/** 按 ui.group 分组（无 group 归"其他"），保字段定义顺序 */
const groupedFields = computed(() => {
  if (!schema.value) return []
  const groups: { name: string; fields: string[] }[] = []
  const index: Record<string, number> = {}
  for (const [key, prop] of Object.entries(schema.value.properties)) {
    const gname = prop.ui?.group ?? '其他'
    if (!(gname in index)) {
      index[gname] = groups.length
      groups.push({ name: gname, fields: [] })
    }
    groups[index[gname]].fields.push(key)
  }
  return groups
})

function getUi(key: string) {
  return schema.value?.properties[key]?.ui ?? {}
}

/** select 控件选项：优先 ui.options（含中文 label），否则用 enum */
function selectOptions(key: string) {
  const prop = schema.value!.properties[key]
  return prop.ui?.options ?? (prop.enum ?? []).map((v) => ({ label: v, value: v }))
}

/** 0-1 浮点字段以百分比展示（slider 0-100），否则原值 */
function isPercent(key: string) {
  const p = schema.value!.properties[key]
  return p.type === 'number' && p.minimum === 0 && p.maximum === 1
}
function toSlider(key: string) {
  return isPercent(key) ? Number(strategyParams[key]) * 100 : Number(strategyParams[key])
}
function toSliderMin(key: string) {
  return isPercent(key) ? 0 : schema.value!.properties[key].minimum ?? 0
}
function toSliderMax(key: string) {
  return isPercent(key) ? 100 : schema.value!.properties[key].maximum ?? 100
}
function fromSlider(key: string, v: number) {
  setField(key, isPercent(key) ? v / 100 : v)
}

function setField(key: string, v: unknown) {
  strategyParams[key] = v
  emit('update', { ...strategyParams })
}
</script>

<style scoped>
.spf-loading, .spf-empty {
  padding: 12px;
  color: #909399;
  font-size: 13px;
  text-align: center;
}
</style>
```

- [ ] **Step 3: 改 `web/src/components/ParamForm.vue`（两段式）**

在 `<script setup>` import 区补：
```typescript
import StrategyParamForm from './StrategyParamForm.vue'
import { getStrategies, type StrategyMeta } from '../api/backtest'
```

在 `formData` reactive 中：删除 `tech_weights: { tech: 0.7, macro: 0.3 }`、`techWeightValue` ref 及其 watch（不再用顶层融合权重）；新增：
```typescript
  // 策略选择（前端驱动调参）
  strategy_name: 'tech_macro_fusion',
  strategy_params: {} as Record<string, unknown>,
```
新增策略列表加载（在 setup 顶层）：
```typescript
const strategies = ref<StrategyMeta[]>([])
getStrategies().then((list) => { strategies.value = list }).catch(() => {})

function onStrategyParamsUpdate(params: Record<string, unknown>) {
  formData.strategy_params = params
}
```

在 `<template>` 中，单资产模式下：删除原"信号融合权重"`el-form-item`（tech_weights 滑块块）；在"信号频率"之后、运行按钮之前插入策略选择 + 动态参数：
```html
    <!-- 策略选择（仅单资产） -->
    <el-form-item v-if="mode === 'single'" label="策略" prop="strategy_name">
      <el-select
        v-model="formData.strategy_name"
        placeholder="选择策略"
        style="width: 100%"
      >
        <el-option
          v-for="s in strategies"
          :key="s.name"
          :label="s.label"
          :value="s.name"
        />
      </el-select>
    </el-form-item>

    <!-- 策略参数（动态 schema 渲染，仅单资产） -->
    <el-form-item v-if="mode === 'single'" label="策略参数">
      <StrategyParamForm
        :strategy-name="formData.strategy_name"
        @update="onStrategyParamsUpdate"
      />
    </el-form-item>
```

`handleSubmit` 的 single 分支改为（删除 `tech_weights`，增 strategy 字段）：
```typescript
  if (props.mode === 'single') {
    emit('submit', {
      symbol: formData.symbol,
      start_date: formData.dateRange[0],
      end_date: formData.dateRange[1],
      initial_capital: formData.initial_capital,
      signal_freq: formData.signal_freq,
      strategy_name: formData.strategy_name,
      strategy_params: formData.strategy_params,
    })
  } else {
    emit('submit', {
      symbols: formData.symbols,
      start_date: formData.dateRange[0],
      end_date: formData.dateRange[1],
      initial_capital: formData.initial_capital,
      n_hmm_states: formData.n_hmm_states,
      buffer_threshold: formData.buffer_threshold,
      state_weights: formData.state_weights,
      strategy_params: formData.strategy_params,
    })
  }
```

组合模式如需暴露 HMM 标量参数，可在 matrix 后追加一个 `<StrategyParamForm strategy-name="hmm_macro" @update="onStrategyParamsUpdate" />`（可选；矩阵仍走现有控件）。

- [ ] **Step 4: 前端构建验证（类型检查 + 构建）**

Run: `cd web && npm run build`
Expected: 构建成功，无 TypeScript 错误（vue-tsc 通过 + vite 产出 dist）

- [ ] **Step 5: 交互验证清单（手动）**

启动后端 `uvicorn server.main:app --reload` + 前端 `cd web && npm run dev`，验证：
- [ ] 单资产页：策略下拉框含"MACD双均线/技术+宏观融合/HMM宏观状态"
- [ ] 切策略：下方 Tab 动态变化（MACD→"MACD均线"组；融合→"均线/量价/宏观/融合"组）
- [ ] 拖动 `tech_weight` 滑块（0-100% 展示）后运行回测，结果与默认不同
- [ ] 改成本模型参数（佣金率）后运行，交易成本变化（Task 6 生效）
- [ ] 缺省提交（不改任何参数）回测正常返回

- [ ] **Step 6: 全量后端回归**

Run: `python -m pytest tests/ -v`
Expected: 全绿

- [ ] **Step 7: 提交**

```bash
git add web/src/api/backtest.ts web/src/components/StrategyParamForm.vue web/src/components/ParamForm.vue
git commit -m "feat(web): 策略参数 JSON Schema 动态渲染器 + ParamForm 两段式重构"
```

---

## 验收标准

- [ ] 每个策略类声明 `params_model`（ClassVar）+ `label`，ctor 收 `params`
- [ ] `GET /api/v1/strategies/{name}/schema` 返回含 `ui` 提示的合法 JSON Schema
- [ ] 前端按 schema 动态渲染 Tab + 控件，无硬编码策略字段
- [ ] 运行回测时 `strategy_params` 经 `params_model` 校验后注入策略；缺省用默认值
- [ ] `tech_weights` 已从 `BacktestRequest` 移除，融合权重经 `TechMacroFusionParams.tech_weight` 下发
- [ ] 单资产默认策略为 `tech_macro_fusion`，保留原 tech+macro 融合行为
- [ ] 组合模式 HMM 训练标量（covariance/n_iter/release_lag/max_fill_days）经 `strategy_params` 可调
- [ ] `run_portfolio` 成本走 `cost_model`（佣金/印花税/过户费可调）
- [ ] `python -m pytest tests/ -v` 全绿
- [ ] `cd web && npm run build` 构建通过
- [ ] 9 个独立 commit

## 后续衔接

- 模块③：把 `_execute_portfolio_order` 整体移入 `BacktestBroker`，接入完整 `CostModel`（含滑点）；`RiskManager` 事前拦截（此时风控/回撤参数 `RiskConfig` 才正式纳入）
- 物理删除 `engine.run()`：模块③ engine 重构时清理（此时 test_backtest 的 Series 测试一并迁移）
- 组合模式 HMM 标量参数的前端控件（可选）：在 PortfolioBacktest 页接入 `<StrategyParamForm strategy-name="hmm_macro">`
