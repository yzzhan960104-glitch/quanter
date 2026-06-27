# 模块② MyTT 指标库 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 自实现通达信/同花顺风格的纯向量化指标库 `factors/mytt.py`（EMA/MA/MACD/BOLL），零第三方依赖，替代手写 Pandas 指标。

**Architecture:** MyTT 本质是通达信公式的 numpy/pandas 翻译。所有函数输入输出均为 `pd.Series`（索引即时间轴），返回带原索引的 Series，天然与系统 OHLCV 对齐。不 `pip install mytt`，守住 CLAUDE.md 反黑盒、第一性原理底线。

**Tech Stack:** Python 3, pandas>=2.0.0, numpy>=1.24.0, pytest>=7.4.0（均已就绪，无需新增依赖）

## Global Constraints

（摘自 spec `2026-06-28-oskhquant-absorb-design.md` 与 CLAUDE.md，每个任务隐式遵循）

- **严禁** 引入任何 PyQt/GUI 代码（架构红线）
- **零新依赖**：本模块仅用 pandas/numpy 标准科学计算栈，**不得** `pip install mytt`
- **全中文注释**：新增代码必须配像素级中文注释（说明 What + Why）
- **纯向量化**：禁用 `for` 循环遍历逐点计算（用 pandas 滚动/ewm 算子）
- **前视偏差**：MyTT 函数本身是纯计算，前视偏差由调用方（策略）用 `shift(1)` 控制
- **通达信约定**：EMA 用 `adjust=False`（递归式）；BOLL 用总体标准差 `ddof=0`；MACD 柱状图 `hist = (dif - dea) * 2`

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `factors/mytt.py` | 通达信指标纯向量化实现（EMA/MA/MACD/BOLL） | 新建 |
| `factors/__init__.py` | 包导出（让 `from factors import EMA` 可用） | 修改（第 12 行后追加 import，`__all__` 追加） |
| `factors/technical.py` | 集成示例：`boll_bands()` 返回标准 DataFrame | 修改（文件末尾追加函数） |
| `tests/test_mytt.py` | MyTT 单元测试（与 `tests/test_factors.py` 同风格） | 新建 |

**依赖说明**：本模块是 5 模块重构的**起点（无依赖）**。后续模块①的策略会调用 `factors.mytt`。本计划不涉及其他模块。

---

## Task 1: `factors/mytt.py` 骨架 + EMA + MA 基础函数

**Files:**
- Create: `factors/mytt.py`
- Test: `tests/test_mytt.py`

**Interfaces:**
- Consumes: 无（起点任务）
- Produces: `EMA(s: pd.Series, n: int) -> pd.Series`、`MA(s: pd.Series, n: int) -> pd.Series`（供 Task 2 MACD、Task 3 BOLL 复用）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_mytt.py`：

```python
"""MyTT 通达信指标库单元测试

覆盖：EMA / MA 基础函数的纯向量化行为与边界。
风格对齐 tests/test_factors.py（class 组织、中文 docstring）。
"""
import numpy as np
import pandas as pd
import pytest

from factors.mytt import EMA, MA


@pytest.fixture
def close_series():
    """构造带趋势的收盘价序列（100 期，tz-aware 索引）"""
    dates = pd.date_range("2023-01-01", periods=100, freq="D", tz="Asia/Shanghai")
    np.random.seed(42)
    prices = 100 + np.cumsum(np.random.randn(100))
    return pd.Series(prices, index=dates, name="close")


class TestEMA:
    """测试指数移动平均（通达信 adjust=False 递归式）"""

    def test_returns_series_with_same_index(self, close_series):
        """返回 Series 且索引与输入一致"""
        ema = EMA(close_series, n=12)
        assert isinstance(ema, pd.Series)
        pd.testing.assert_index_equal(ema.index, close_series.index)

    def test_first_value_equals_first_input(self):
        """adjust=False：EMA 首值 = 输入首值"""
        s = pd.Series([10.0, 20.0, 30.0, 40.0])
        ema = EMA(s, n=5)
        assert ema.iloc[0] == pytest.approx(10.0)

    def test_no_nan_in_output(self, close_series):
        """EMA 递归式全程有值，无 NaN"""
        ema = EMA(close_series, n=12)
        assert not ema.isna().any()

    def test_smoother_than_raw(self, close_series):
        """EMA 比原始序列更平滑（标准差更小）"""
        ema = EMA(close_series, n=12)
        assert ema.std() < close_series.std()


class TestMA:
    """测试简单移动平均"""

    def test_returns_series_with_same_index(self, close_series):
        """返回 Series 且索引与输入一致"""
        ma = MA(close_series, n=5)
        assert isinstance(ma, pd.Series)
        pd.testing.assert_index_equal(ma.index, close_series.index)

    def test_window_mean_exact(self):
        """MA(3) 第 3 个值 = 前 3 个值的均值"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        ma = MA(s, n=3)
        assert ma.iloc[2] == pytest.approx(2.0)

    def test_has_nan_before_window(self):
        """MA(3) 前 2 个值为 NaN（窗口未满）"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        ma = MA(s, n=3)
        assert ma.iloc[:2].isna().all()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_mytt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'factors.mytt'`

- [ ] **Step 3: 写最小实现**

新建 `factors/mytt.py`：

```python
"""MyTT —— 通达信/同花顺指标库的 Python 纯向量化实现

设计哲学（对齐 CLAUDE.md 反黑盒、第一性原理）：
- 不引入第三方 mytt 包，自行用 pandas/numpy 逐函数翻译通达信公式
- 所有函数输入输出均为 pd.Series，索引即时间轴，天然对齐 OHLCV
- 纯向量化（rolling / ewm 算子），禁用 for 循环逐点计算

通达信约定（与同花顺一致，区别于部分西方指标库）：
- EMA 用 adjust=False 的递归式：y_0 = x_0; y_t = α·x_t + (1-α)·y_{t-1}, α = 2/(n+1)
- BOLL 用总体标准差 ddof=0
- MACD 柱状图 hist = (DIF - DEA) * 2

前视偏差说明：
本模块是纯数学计算，不涉及时间位移。前视偏差由调用方（策略层）用 shift(1) 控制，
与现有 factors/technical.py 的处理方式一致。
"""
import pandas as pd


def EMA(s: pd.Series, n: int) -> pd.Series:
    """
    指数移动平均（通达信递归式）

    物理含义：对近期数据赋予指数衰减的更高权重，比简单移动平均对价格变化更敏感、更平滑。
    通达信约定 adjust=False：首个值直接取输入首值，后续按递归式展开，
    避免adjust=True 在序列起始处的"归一化偏移"。

    参数：
        s: 输入序列（通常为 close）
        n: 计算周期（span）

    返回：
        与 s 同索引的 EMA 序列（全程无 NaN）
    """
    return s.ewm(span=n, adjust=False).mean()


def MA(s: pd.Series, n: int) -> pd.Series:
    """
    简单移动平均（Simple Moving Average）

    物理含义：过去 n 期的算术平均，最基础的均线。
    前 n-1 个值为 NaN（窗口未满），由调用方决定如何填充（ffill 或丢弃）。

    参数：
        s: 输入序列
        n: 计算周期

    返回：
        与 s 同索引的 MA 序列（前 n-1 个为 NaN）
    """
    return s.rolling(window=n).mean()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_mytt.py -v`
Expected: PASS — 7 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add factors/mytt.py tests/test_mytt.py
git commit -m "feat(factors): 新增 MyTT EMA/MA 纯向量化基础函数"
```

---

## Task 2: MACD 指标

**Files:**
- Modify: `factors/mytt.py`（文件末尾追加 `MACD`）
- Test: `tests/test_mytt.py`（追加 `TestMACD` 类）

**Interfaces:**
- Consumes: `EMA`（来自 Task 1）
- Produces: `MACD(close: pd.Series, fast=12, slow=26, signal=9) -> tuple[pd.Series, pd.Series, pd.Series]`，返回 `(DIF, DEA, HIST)`

- [ ] **Step 1: 写失败测试**

在 `tests/test_mytt.py` 末尾追加（import 行已含 `from factors.mytt import EMA, MA`，本步需补充导入 MACD）：

先把第 9 行 import 改为：
```python
from factors.mytt import EMA, MA, MACD
```

再追加测试类：
```python
class TestMACD:
    """测试 MACD（通达信约定 hist = (DIF-DEA)*2）"""

    def test_returns_three_series(self, close_series):
        """返回三个 Series：DIF / DEA / HIST"""
        dif, dea, hist = MACD(close_series)
        assert isinstance(dif, pd.Series)
        assert isinstance(dea, pd.Series)
        assert isinstance(hist, pd.Series)

    def test_index_matches_input(self, close_series):
        """三条线索引与输入一致"""
        dif, dea, hist = MACD(close_series)
        pd.testing.assert_index_equal(dif.index, close_series.index)
        pd.testing.assert_index_equal(hist.index, close_series.index)

    def test_hist_formula_double_diff(self, close_series):
        """HIST = (DIF - DEA) * 2（通达信柱状图放大 2 倍约定）"""
        dif, dea, hist = MACD(close_series)
        expected = (dif - dea) * 2
        # 比较 non-NaN 部分（前段因 slow EMA 暖机可能有 NaN）
        pd.testing.assert_series_equal(
            hist.dropna(), expected.dropna(), check_names=False
        )

    def test_dif_is_fast_minus_slow_ema(self, close_series):
        """DIF = EMA(close, fast) - EMA(close, slow)"""
        dif, _, _ = MACD(close_series, fast=12, slow=26, signal=9)
        expected = EMA(close_series, 12) - EMA(close_series, 26)
        pd.testing.assert_series_equal(dif, expected, check_names=False)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_mytt.py::TestMACD -v`
Expected: FAIL — `ImportError: cannot import name 'MACD' from 'factors.mytt'`

- [ ] **Step 3: 写最小实现**

在 `factors/mytt.py` 末尾追加：

```python
def MACD(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    MACD（Moving Average Convergence Divergence，指数平滑异同移动平均）

    通达信公式：
        DIF = EMA(close, fast) - EMA(close, slow)   # 快慢均线差，MACD 主线
        DEA = EMA(DIF, signal)                       # 信号线（对 DIF 再求 EMA，非对 close）
        HIST = (DIF - DEA) * 2                        # 柱状图（通达信约定 ×2 放大）

    关键点：DEA 是对 DIF 求 EMA，不是对 close 求 EMA。这是新手最易写错处。

    参数：
        close: 收盘价序列
        fast: 快线周期（默认 12）
        slow: 慢线周期（默认 26）
        signal: 信号线周期（默认 9）

    返回：
        (DIF, DEA, HIST) 三条 pd.Series，索引均与 close 一致
    """
    dif = EMA(close, fast) - EMA(close, slow)
    dea = EMA(dif, signal)          # 关键：对 DIF 求 EMA，不是对 close
    hist = (dif - dea) * 2          # 通达信柱状图约定 ×2
    return dif, dea, hist
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_mytt.py::TestMACD -v`
Expected: PASS — 4 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add factors/mytt.py tests/test_mytt.py
git commit -m "feat(factors): 新增 MyTT MACD 指标（通达信 hist×2 约定）"
```

---

## Task 3: BOLL 布林带指标

**Files:**
- Modify: `factors/mytt.py`（末尾追加 `BOLL`）
- Test: `tests/test_mytt.py`（追加 `TestBOLL` 类，补充导入）

**Interfaces:**
- Consumes: `MA`（来自 Task 1）
- Produces: `BOLL(close: pd.Series, n=20, p=2) -> tuple[pd.Series, pd.Series, pd.Series]`，返回 `(UPPER, MID, LOWER)`

- [ ] **Step 1: 写失败测试**

把 import 行改为：
```python
from factors.mytt import EMA, MA, MACD, BOLL
```

在 `tests/test_mytt.py` 末尾追加：
```python
class TestBOLL:
    """测试布林带（通达信约定总体标准差 ddof=0）"""

    def test_returns_three_series(self, close_series):
        """返回三个 Series：UPPER / MID / LOWER"""
        upper, mid, lower = BOLL(close_series)
        assert isinstance(upper, pd.Series)
        assert isinstance(mid, pd.Series)
        assert isinstance(lower, pd.Series)

    def test_mid_equals_ma(self, close_series):
        """MID = MA(close, n)"""
        upper, mid, lower = BOLL(close_series, n=20, p=2)
        pd.testing.assert_series_equal(
            mid.dropna(), MA(close_series, 20).dropna(), check_names=False
        )

    def test_upper_lower_symmetric(self, close_series):
        """UPPER - MID == MID - LOWER（p 倍标准差两侧对称）"""
        upper, mid, lower = BOLL(close_series, n=20, p=2)
        diff_up = (upper - mid).dropna()
        diff_low = (mid - lower).dropna()
        pd.testing.assert_series_equal(diff_up, diff_low, check_names=False)

    def test_uses_population_std(self, close_series):
        """UPPER = MA + p × 总体标准差（ddof=0），验证非样本标准差"""
        upper, mid, lower = BOLL(close_series, n=20, p=2)
        std_pop = close_series.rolling(20).std(ddof=0)   # 总体标准差
        expected_upper = MA(close_series, 20) + 2 * std_pop
        pd.testing.assert_series_equal(
            upper.dropna(), expected_upper.dropna(), check_names=False
        )

    def test_upper_above_mid_above_lower(self, close_series):
        """UPPER ≥ MID ≥ LOWER（在非 NaN 区间成立）"""
        upper, mid, lower = BOLL(close_series, n=20, p=2)
        valid = mid.notna()
        assert (upper[valid] >= mid[valid]).all()
        assert (mid[valid] >= lower[valid]).all()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_mytt.py::TestBOLL -v`
Expected: FAIL — `ImportError: cannot import name 'BOLL' from 'factors.mytt'`

- [ ] **Step 3: 写最小实现**

在 `factors/mytt.py` 末尾追加：
```python
def BOLL(close: pd.Series, n: int = 20, p: int = 2):
    """
    布林带（Bollinger Bands）

    通达信公式：
        MID   = MA(close, n)
        UPPER = MID + p × STD(close, n)
        LOWER = MID - p × STD(close, n)
    其中 STD 用总体标准差（ddof=0）——这是通达信约定，
    不同于 pandas 默认的样本标准差（ddof=1），差 1 个自由度。

    物理含义：价格在 ±p 倍标准差通道内波动，触及上下轨常作超买/超卖研判。

    参数：
        close: 收盘价序列
        n: 计算周期（默认 20）
        p: 标准差倍数（默认 2）

    返回：
        (UPPER, MID, LOWER) 三条 pd.Series，索引均与 close 一致（前 n-1 个为 NaN）
    """
    mid = MA(close, n)
    std = close.rolling(n).std(ddof=0)   # 通达信用总体标准差
    upper = mid + p * std
    lower = mid - p * std
    return upper, mid, lower
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_mytt.py::TestBOLL -v`
Expected: PASS — 5 个测试全绿

- [ ] **Step 5: 提交**

```bash
git add factors/mytt.py tests/test_mytt.py
git commit -m "feat(factors): 新增 MyTT BOLL 布林带（ddof=0 总体标准差）"
```

---

## Task 4: 包导出 + `technical.py` 集成（`boll_bands` + `macd_table`）

**Files:**
- Modify: `factors/__init__.py`（第 12 行后追加 mytt 导入 + `__all__` 追加）
- Modify: `factors/technical.py`（文件末尾追加 `boll_bands` 与 `macd_table`）
- Test: `tests/test_factors.py`（追加 `TestBollBands` 与 `TestMacdTable` 类）

**Interfaces:**
- Consumes: `BOLL`、`MACD`（来自 Task 2/3）
- Produces:
  - `factors.boll_bands(df, n, p) -> pd.DataFrame`（列 `['upper', 'mid', 'lower']`）
  - `factors.macd_table(df, fast, slow, signal) -> pd.DataFrame`（列 `['dif', 'dea', 'hist']`）

- [ ] **Step 1: 写失败测试**

在 `tests/test_factors.py` 顶部 import 区（第 14 行 `from factors import moving_average_cross, ...` 附近）追加：
```python
from factors import boll_bands, macd_table
```

在 `tests/test_factors.py` 末尾追加：
```python
class TestBollBands:
    """测试 BOLL 集成封装（返回标准 DataFrame）"""

    def test_returns_dataframe(self, sample_df):
        """返回 DataFrame"""
        bands = boll_bands(sample_df)
        assert isinstance(bands, pd.DataFrame)

    def test_has_three_columns(self, sample_df):
        """含 upper / mid / lower 三列"""
        bands = boll_bands(sample_df)
        assert list(bands.columns) == ["upper", "mid", "lower"]

    def test_index_matches_input(self, sample_df):
        """索引与输入 df 一致"""
        bands = boll_bands(sample_df)
        pd.testing.assert_index_equal(bands.index, sample_df.index)

    def test_upper_above_mid_above_lower(self, sample_df):
        """UPPER ≥ MID ≥ LOWER（非 NaN 区间）"""
        bands = boll_bands(sample_df, n=20, p=2)
        valid = bands["mid"].notna()
        assert (bands.loc[valid, "upper"] >= bands.loc[valid, "mid"]).all()
        assert (bands.loc[valid, "mid"] >= bands.loc[valid, "lower"]).all()


class TestMacdTable:
    """测试 MACD 集成封装（返回标准 DataFrame）"""

    def test_returns_dataframe(self, sample_df):
        """返回 DataFrame"""
        table = macd_table(sample_df)
        assert isinstance(table, pd.DataFrame)

    def test_has_three_columns(self, sample_df):
        """含 dif / dea / hist 三列"""
        table = macd_table(sample_df)
        assert list(table.columns) == ["dif", "dea", "hist"]

    def test_index_matches_input(self, sample_df):
        """索引与输入 df 一致"""
        table = macd_table(sample_df)
        pd.testing.assert_index_equal(table.index, sample_df.index)

    def test_hist_is_double_diff(self, sample_df):
        """hist = (dif - dea) * 2（通达信约定）"""
        table = macd_table(sample_df).dropna()
        expected = (table["dif"] - table["dea"]) * 2
        pd.testing.assert_series_equal(table["hist"], expected, check_names=False)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_factors.py::TestBollBands tests/test_factors.py::TestMacdTable -v`
Expected: FAIL — `ImportError: cannot import name 'boll_bands' from 'factors'`

- [ ] **Step 3a: 在 `factors/__init__.py` 导出 mytt 函数**

把 `factors/__init__.py` 第 12 行：
```python
from .technical import moving_average_cross, volume_price_trend, rsi, macd
```
改为：
```python
from .technical import moving_average_cross, volume_price_trend, rsi, macd, boll_bands, macd_table
from .mytt import EMA, MA, MACD, BOLL
```

并在 `__all__` 列表（第 20-37 行）的 `"macd",` 之后追加：
```python
    "boll_bands",
    "macd_table",
    "EMA",
    "MA",
    "MACD",
    "BOLL",
```

- [ ] **Step 3b: 在 `factors/technical.py` 末尾追加 `boll_bands` 与 `macd_table`**

在 `factors/technical.py` 文件末尾（`macd` 函数之后）追加：
```python
def boll_bands(
    df: pd.DataFrame,
    n: int = 20,
    p: int = 2
) -> pd.DataFrame:
    """
    布林带（封装 MyTT.BOLL 为标准 DataFrame 输出）

    用途：策略层（如 BollStrategy）直接消费宽表格式，
    相比返回三个 Series 更便于与 OHLCV 拼接。

    参数：
        df: OHLCV 数据（需包含 'close' 列）
        n: 布林带周期（默认 20）
        p: 标准差倍数（默认 2）

    返回：
        DataFrame，列为 ['upper', 'mid', 'lower']，索引与 df 一致
    """
    from .mytt import BOLL
    upper, mid, lower = BOLL(df["close"], n=n, p=p)
    return pd.DataFrame(
        {"upper": upper, "mid": mid, "lower": lower},
        index=df.index
    )


def macd_table(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> pd.DataFrame:
    """
    MACD（封装 MyTT.MACD 为标准 DataFrame 输出）

    与本文件已有的手写 macd() 信号函数区别：
    - macd()：返回 [0,1] 仓位信号（金叉/死叉判定），面向回测引擎
    - macd_table()：返回原始 DIF/DEA/HIST 三线，面向策略层自行研判
    二者职责不同，并存不冲突（YAGNI：不重构现有 macd()）。

    参数：
        df: OHLCV 数据（需包含 'close' 列）
        fast: 快线周期（默认 12）
        slow: 慢线周期（默认 26）
        signal: 信号线周期（默认 9）

    返回：
        DataFrame，列为 ['dif', 'dea', 'hist']，索引与 df 一致
    """
    from .mytt import MACD
    dif, dea, hist = MACD(df["close"], fast=fast, slow=slow, signal=signal)
    return pd.DataFrame(
        {"dif": dif, "dea": dea, "hist": hist},
        index=df.index
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_factors.py::TestBollBands tests/test_factors.py::TestMacdTable tests/test_mytt.py -v`
Expected: PASS — 新增 8 个 + 原有 MyTT 测试全绿

- [ ] **Step 5: 全量回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿（确认 mytt 新增未破坏现有 factors/backtest/data/trading/viz 测试）

- [ ] **Step 6: 提交**

```bash
git add factors/__init__.py factors/technical.py tests/test_factors.py
git commit -m "feat(factors): 导出 MyTT 函数并集成 boll_bands/macd_table 到 technical"
```

---

## 验收标准

- [ ] `factors/mytt.py` 含 `EMA/MA/MACD/BOLL` 四函数，零第三方依赖
- [ ] `from factors import EMA, MA, MACD, BOLL, boll_bands` 可用
- [ ] `python -m pytest tests/ -v` 全绿
- [ ] 所有新增代码配像素级中文注释（含 Why）
- [ ] 4 个独立 commit，每个对应一个任务

## 后续衔接

本模块完成后，即可推进**模块① 策略插件系统**的计划（`MaCrossStrategy`/`BollStrategy` 将调用 `factors.mytt`）。按 spec 6.5 依赖顺序，下一份 plan 为模块①。
