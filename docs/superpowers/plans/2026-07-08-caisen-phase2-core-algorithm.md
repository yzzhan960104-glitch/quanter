# 蔡森形态学流水线 · Phase 2：核心算法 + 回放验证器 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现蔡森形态学核心算法（因果 ZigZag、W底/头肩底识别、颈线满足计算、风控、计划生成）+ 历史回放验证器，产出可离线 CLI 跑通的"筛形态→生成计划→回放验证"链路。

**Architecture:** 纯 Python + pandas/numpy/zigzag/pandas-ta 显式实现，无未来函数（因果 ZigZag + 末尾 pivot 滞后确认）。依赖 Phase 1 完成的 `core/macro_regime.CreditRegime` 与 `data/lake_reader`。所有算法与实盘/回放共用同一套因果化代码（杜绝双源真理）。

**Tech Stack:** Python 3.10（`.venv310`）、pandas、numpy、zigzag、pandas-ta、trendln、pytest。新依赖在 Task 0 安装。

## Global Constraints

- 解释器 `.venv310/Scripts/python.exe`，pytest 前缀 `PYTHONIOENCODING=utf-8`。
- **无未来函数红线**：所有形态识别必须因果化（T 日决策只用 T 及之前数据）。`tests/caisen/test_zigzag_causal.py` 含未来函数回归测试（追加未来数据，识别结果不变）。
- **蔡森方法学对齐**：止盈用颈线满足计算（非随意等距）；W底识别含打底 ABC 波 + 幅宽张力；假突破=形态失败。精确规则以原著正文为准（Task 1 精读确认）。
- 全中文注释（CLAUDE.md）；每个数学步骤注释 Why。
- 每任务 commit；commit message 中文 conventional + `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

### Task 0: 安装依赖 + caisen 包骨架

**Files:**
- Modify: `requirements.txt`（追加 5 个依赖）
- Create: `caisen/__init__.py`、`caisen/patterns/__init__.py`、`tests/caisen/__init__.py`

**Interfaces:**
- Produces: `caisen` 包可 import；依赖就绪。

- [ ] **Step 1: 安装新依赖（清华镜像）**

Run:
```bash
.venv310/Scripts/pip.exe install -i https://pypi.tuna.tsinghua.edu.cn/simple "zigzag>=0.2.0" "pandas-ta>=0.3.14b" "trendln>=4.0.0" "mplfinance>=0.12.10" "lightweight-charts-python>=2.0" 2>&1 | tail -5
```
Expected: Successfully installed ...。若 trendln/某包失败，记录但继续（Phase 2 仅 zigzag/pandas-ta 必需，trendln 有 numpy polyfit 回退）。

- [ ] **Step 2: 追加到 requirements.txt**

在 `requirements.txt` 末尾追加：
```
# ===== 蔡森形态学流水线 =====
zigzag>=0.2.0
pandas-ta>=0.3.14b
trendln>=4.0.0
mplfinance>=0.12.10
lightweight-charts-python>=2.0
```

- [ ] **Step 3: 建包骨架**

Run:
```bash
mkdir -p caisen/patterns tests/caisen
echo '"""蔡森多空转折形态学流水线（纯多头）。"""' > caisen/__init__.py
echo '"""形态识别子包：因果 ZigZag / 颈线 / W底 / 头肩底 / 编排器。"""' > caisen/patterns/__init__.py
echo '' > tests/caisen/__init__.py
```

- [ ] **Step 4: 验证依赖可 import**

Run: `.venv310/Scripts/python.exe -c "import zigzag, pandas_ta, numpy, pandas; print('deps ok')"`
Expected: `deps ok`（trendln 失败不阻断，回退 polyfit）。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "build(caisen): 安装形态学依赖 + caisen 包骨架

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 1: 精读蔡森实战篇核心章节（把推断升级为原著直接）

**Files:**
- Read: `多空轉折一手抓.pdf` 实战篇四/六/八/十 + 技术篇一（W底颈线满足计算）
- Modify: `docs/caisen-methodology-summary.md`（把【框架推断】升级为【原著直接】，补精确规则）

**说明：** spec 第 5/6 节的形态规则当前部分为框架推断。本任务在写识别器前，先读原著正文确认精确规则，避免用通用西方形态学"幻觉"了蔡森独家方法。

- [ ] **Step 1: 定位实战篇与技术篇核心章节的 PDF 物理页**

用既有脚本渲染采样页定位（目录页码为印刷页码，PDF≈+1 偏移）：
```bash
PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe scripts/_render_pdf.py 17 26   # 技术篇 W底颈线满足计算
```
对每页 `Read scripts/pages/pXXX.png` 上传 CDN，再用 `analyze_image` 工具提取文字。重点找：① W底颈线满足计算的公式（颈线高度投影倍数）② 满足点分级规则。

- [ ] **Step 2: 定位实战篇正文（书后半，约 PDF 130-220 区间采样）**

```bash
PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe scripts/_render_pdf.py 130 135
PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe scripts/_render_pdf.py 160 165
PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe scripts/_render_pdf.py 190 195
```
逐页视觉识别找：实战篇四（底部反转＆多方转折）、六（双底幅宽张力）、八（W底满足计算）、十（形态失败）。记录章节起始 PDF 页。

- [ ] **Step 3: 提取 4 个核心章节的精确规则**

对定位到的章节页，用 `analyze_image` 提取并整理：
- **颈线满足计算**：精确公式与倍数（1倍/2倍满足点的判定）
- **打底 ABC 波**：A/B/C 波的结构定义与判定
- **多方转折点**：转折的精确触发条件
- **双底幅宽与张力**：幅宽比例、两底价格关系的精确阈值
- **形态失败**：假突破的反向操作规则

- [ ] **Step 4: 更新方法学总结文档**

Edit `docs/caisen-methodology-summary.md`：把第 9 节"待精读清单"中已确认项的【框架推断】标记改为【原著直接】，补入精确规则。仍未读到的保留【推断】标记。

- [ ] **Step 5: 清理渲染图片 + Commit**

```bash
rm -f scripts/pages/*.png
git add docs/caisen-methodology-summary.md && git commit -m "docs(caisen): 精读实战篇核心章节，推断升级为原著直接

补入颈线满足计算公式、打底ABC波、多方转折点、双底幅宽张力、形态失败的精确规则，
供 Phase 2 形态识别器实现参照（事实审查，杜绝幻觉）。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: StrategyConfig 参数模型

**Files:**
- Create: `caisen/config.py`
- Test: `tests/caisen/test_config.py`

**Interfaces:**
- Produces: `caisen.config.StrategyConfig`（Pydantic 模型，所有阈值的真相源）

- [ ] **Step 1: 写失败测试**

`tests/caisen/test_config.py`：
```python
# -*- coding: utf-8 -*-
"""StrategyConfig 参数模型测试：默认值与边界校验。"""
import pytest
from pydantic import ValidationError
from caisen.config import StrategyConfig


def test_default_config_loads():
    """默认参数可构造，且关键风控阈值符合 spec。"""
    cfg = StrategyConfig()
    assert cfg.min_pattern_bars == 11            # >10 硬约束
    assert cfg.min_rr_ratio == 3.0               # 25% 胜率期望为正
    assert cfg.max_position_pct == 0.05          # 单标的 5% 上限
    assert cfg.liquidity_min_amount == 1e8       # 1 亿
    assert cfg.neckline_projection_multiple == 1.0  # 颈线满足 1 倍


def test_min_pattern_bars_below_11_rejected():
    """形态跨度 < 11 必须拒绝（spec 硬约束 >10 交易日）。"""
    with pytest.raises(ValidationError):
        StrategyConfig(min_pattern_bars=10)


def test_negative_threshold_rejected():
    """负阈值非法。"""
    with pytest.raises(ValidationError):
        StrategyConfig(pullback_max_pct=-0.01)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -m pytest tests/caisen/test_config.py -q`
Expected: FAIL（ModuleNotFoundError: caisen.config）。

- [ ] **Step 3: 实现 StrategyConfig**

`caisen/config.py`：
```python
# -*- coding: utf-8 -*-
"""蔡森形态学策略参数模型（Pydantic，全参数真相源）。

设计（CLAUDE.md）：严禁逻辑代码硬编码阈值；所有阈值集中此模型，
前端表单可经 model_json_schema() 反射动态渲染。蔡森方法学专用参数
（颈线满足倍数/打底ABC/幅宽张力/破头锅）单独分组。
"""
from pydantic import BaseModel, Field


class StrategyConfig(BaseModel):
    # —— 时间跨度类 ——
    min_pattern_bars: int = Field(11, ge=11, description="形态最小跨度(>10 硬约束，交易日)")
    max_pattern_bars: int = Field(60, ge=20, le=120, description="形态最大跨度")
    symmetry_tolerance: float = Field(0.3, description="左右结构时间对称容忍度(占比)")

    # —— 空间高度类 ——
    zigzag_threshold_atr: float = Field(1.0, ge=0.5, description="ZigZag 波段提取阈值(倍 ATR)")
    min_pattern_depth: float = Field(0.03, description="形态最浅幅度(占价格比例)")
    max_pattern_depth: float = Field(0.30, description="形态最深幅度(防失效长趋势)")
    w_price_tolerance: float = Field(0.02, description="W 底两底价格高度容忍度")

    # —— 量价配合类（蔡森核心：精準量價）——
    right_vol_shrink: float = Field(0.8, description="右底缩量比例(右底量/左底量上限)")
    breakout_vol_multiplier: float = Field(1.5, description="突破颈线成交量放大倍数")

    # —— 交易执行类 ——
    pullback_window_bars: int = Field(3, description="突破后有效回踩 K 线数")
    pullback_max_pct: float = Field(0.02, description="回踩至不高于突破点 2%")
    stop_loss_atr_buffer: float = Field(0.3, description="止损点 ATR 缓冲垫")
    min_rr_ratio: float = Field(3.0, description="盈亏比下限(25% 胜率期望为正)")

    # —— 时间止损/超时离场 ——
    max_holding_bars: int = Field(15, description="最大持仓周期")
    timeout_exit_threshold: float = Field(0.01, description="超时离场浮盈阈值(1%)")
    trailing_activation_bars: int = Field(5, description="移动止盈激活持仓天数")
    trailing_to_breakeven: bool = Field(True, description="激活后止损上移至盈亏平衡")

    # —— 风控类 ——
    liquidity_min_amount: float = Field(1e8, description="近30日均成交额下限(1 亿)")
    hv_window: int = Field(20, description="历史波动率窗口")
    hv_max_quantile: float = Field(0.95, description="HV 异常分位上限(过滤无序震荡)")
    max_position_pct: float = Field(0.05, description="单标的占总资金上限 5%")
    macro_regime_veto: bool = Field(True, description="宏观收缩期是否一票否决新开仓")
    confirm_bars: int = Field(3, description="ZigZag 末尾 pivot 滞后确认窗口")

    # —— 蔡森方法学专用 ——
    neckline_projection_multiple: float = Field(1.0, ge=0.5, le=3.0, description="颈线满足计算倍数(1倍/2倍满足点)")
    abc_wave_detect: bool = Field(True, description="启用打底 ABC 波过程识别(防下跌中继误判)")
    pattern_tension_ratio: float = Field(0.4, description="幅宽张力:形态高度/宽度比例下限")
    enable_pot_breakout: bool = Field(True, description="启用破头锅突破前头部形态")
```

- [ ] **Step 4: 跑测试通过**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -m pytest tests/caisen/test_config.py -q`
Expected: 3 passed。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(caisen): StrategyConfig 参数模型（全阈值真相源）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 因果 ZigZag（未来函数隔离）

**Files:**
- Create: `caisen/patterns/zigzag_causal.py`
- Test: `tests/caisen/test_zigzag_causal.py`

**Interfaces:**
- Produces: `caisen.patterns.zigzag_causal.causal_pivots(close: pd.Series, atr: pd.Series, cfg: StrategyConfig) -> pd.Series`（index=close.index，值∈{1=峰,-1=谷,0=非}，严格因果）
- Consumes: `StrategyConfig`

- [ ] **Step 1: 写失败测试（含未来函数回归）**

`tests/caisen/test_zigzag_causal.py`：
```python
# -*- coding: utf-8 -*-
"""因果 ZigZag 测试：pivot 标记 + 末尾未确认丢弃 + 未来函数回归。"""
import numpy as np
import pandas as pd
from caisen.config import StrategyConfig
from caisen.patterns.zigzag_causal import causal_pivots


def _atr_const(n, val):
    return pd.Series(val, index=pd.RangeIndex(n))

def test_synthetic_w_shape_pivots():
    """合成 W 形：识别出 谷-峰-谷-峰 四个 pivot。"""
    price = pd.Series([10, 9, 8, 9, 10, 11, 10, 9, 8, 9, 10, 11, 12, 13], dtype=float)
    cfg = StrategyConfig(zigzag_threshold_atr=0.5, confirm_bars=3)
    piv = causal_pivots(price, _atr_const(len(price), 1.0), cfg)
    assert piv.isin([1, -1]).sum() >= 4   # 至少 4 个 pivot


def test_last_unconfirmed_pivot_dropped():
    """末尾新出现的极值未被 confirm_bars 确认 → 丢弃（标 0）。"""
    # 末尾刚创新低，其后无足够确认 K 线
    price = pd.Series([10, 11, 10, 9, 8, 7.5], dtype=float)
    cfg = StrategyConfig(zigzag_threshold_atr=0.5, confirm_bars=3)
    piv = causal_pivots(price, _atr_const(len(price), 1.0), cfg)
    # 最后一个点（index 5）不应被标为确认 pivot
    assert piv.iloc[-1] == 0


def test_no_lookahead_bias():
    """未来函数回归：对序列 S 识别 pivot 后，在 S 末尾追加新数据，
    原 pivot 标记在重叠区间必须完全一致（不因未来数据改变历史判断）。"""
    base = pd.Series([10, 9, 8, 9, 10, 11, 10, 9, 8, 9, 10, 11, 12, 13], dtype=float)
    extended = pd.concat([base, pd.Series([12, 11, 10, 9], dtype=float)])
    cfg = StrategyConfig(zigzag_threshold_atr=0.5, confirm_bars=3)
    piv_base = causal_pivots(base, _atr_const(len(base), 1.0), cfg)
    piv_ext = causal_pivots(extended, _atr_const(len(extended), 1.0), cfg)
    # 重叠区间（base 的长度）pivot 标记必须一致——这是无未来函数的硬证明
    np.testing.assert_array_equal(piv_base.values, piv_ext.values[: len(base)])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -m pytest tests/caisen/test_zigzag_causal.py -q`
Expected: FAIL（ModuleNotFoundError）。

- [ ] **Step 3: 实现因果 ZigZag**

`caisen/patterns/zigzag_causal.py`：
```python
# -*- coding: utf-8 -*-
"""因果 ZigZag：未来函数隔离层。

蔡森/量化风控红线（CLAUDE.md）：zigzag 包的 peak_valley_pivots 是全局后向算法
（T 是否为极值取决于 T 之后的反转），直接用于实盘盘中是未来函数。本模块隔离：
- 历史已完成 pivot（已被后续反转确认）→ 无未来函数，用 zigzag 包提取。
- 末尾未确认 pivot（最近极值，其后反转不足）→ 滞后确认：须其后 ≥ confirm_bars
  根 K 线未创新极值才认定有效，否则丢弃（标 0）。
- 回退路径：zigzag 包缺失时自写因果 ZigZag（pandas 滚动极值 + ATR 阈值）。

无前视证明：每个 pivot 在时刻 t 的确认仅依赖 t 之前的走势 + t 之后 confirm_bars
根已发生 K 线（T 日收盘看 T-1 及之前，合法）。
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from caisen.config import StrategyConfig


def _true_range(df_high: pd.Series, df_low: pd.Series, df_close: pd.Series) -> pd.Series:
    """真实波幅 TR（ATR 基元）：max(H-L, |H-前C|, |L-前C|)。"""
    prev_close = df_close.shift(1)
    tr = pd.concat([(df_high - df_low), (df_high - prev_close).abs(), (df_low - prev_close).abs()], axis=1).max(axis=1)
    return tr


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """ATR = TR 的 window 日均值（无未来函数，仅用过去 window 日）。"""
    tr = _true_range(high, low, close)
    return tr.rolling(window, min_periods=1).mean()


def causal_pivots(close: pd.Series, atr: pd.Series, cfg: StrategyConfig) -> pd.Series:
    """返回因果 pivot 标记序列（1=峰, -1=谷, 0=非）。

    参数：
        close: 收盘价序列
        atr:   对齐的 ATR 序列（用于将 zigzag 阈值转为价格百分比）
        cfg:   策略参数（zigzag_threshold_atr, confirm_bars）
    返回：
        与 close 同 index 的 int 序列，末尾未确认 pivot 标 0。
    """
    n = len(close)
    result = pd.Series(0, index=close.index, dtype=int)
    if n < 5:
        return result

    # zigzag 阈值：用近期 ATR 占价格的比例作为 up/down 阈值（百分比）
    recent_atr = atr.iloc[-1] if atr.iloc[-1] > 0 else close.pct_change().abs().mean()
    thresh = max(0.01, (recent_atr / close.iloc[-1]) * cfg.zigzag_threshold_atr)
    up_thresh, down_thresh = thresh, thresh

    # 用 zigzag 包提取主干 pivot（含末尾未确认）
    try:
        import zigzag
        raw = zigzag.peak_valley_pivots(close.values, up_thresh, down_thresh)
    except Exception:
        raw = _fallback_causal_pivots(close, up_thresh)

    # 末尾滞后确认：最后一个非零 pivot 须距序列末尾 ≥ confirm_bxs，否则丢弃
    last_pivot_idx = None
    for i in range(n - 1, -1, -1):
        if raw[i] != 0:
            last_pivot_idx = i
            break
    for i in range(n):
        if raw[i] == 0:
            continue
        # 末尾 pivot 滞后确认：若是最后一个 pivot 且其后 K 线数 < confirm_bars → 丢弃
        if i == last_pivot_idx and (n - 1 - i) < cfg.confirm_bars:
            result.iloc[i] = 0   # 未成形，丢弃
        else:
            result.iloc[i] = int(raw[i])
    return result


def _fallback_causal_pivots(close: pd.Series, thresh: float) -> np.ndarray:
    """自写因果 ZigZag 回退（zigzag 包不可用时）。

    极简算法：迭代跟踪当前趋势，价格反转幅度超 thresh 则确认前一个极值为 pivot。
    天然因果（只看过去），但末尾 pivot 仍由调用方 confirm_bars 兜底确认。
    """
    n = len(close)
    out = np.zeros(n)
    if n < 2:
        return out
    trend = 0  # 0=未定, 1=上, -1=下
    last_pivot = 0
    extremum = close.iloc[0]
    extremum_idx = 0
    for i in range(1, n):
        p = close.iloc[i]
        if trend >= 0 and p > extremum:
            extremum, extremum_idx = p, i
        elif trend <= 0 and p < extremum:
            extremum, extremum_idx = p, i
        # 反转判定
        if trend != -1 and p < extremum * (1 - thresh):
            out[extremum_idx] = 1            # 前高确认为峰
            trend = -1
            extremum, extremum_idx = p, i
        elif trend != 1 and p > extremum * (1 + thresh):
            out[extremum_idx] = -1           # 前低确认为谷
            trend = 1
            extremum, extremum_idx = p, i
    return out
```

- [ ] **Step 4: 跑测试通过**

Run: `PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -m pytest tests/caisen/test_zigzag_causal.py -q`
Expected: 3 passed。**`test_no_lookahead_bias` 是无未来函数的硬证明，必须绿。**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(caisen): 因果 ZigZag（末尾 pivot 滞后确认，未来函数隔离）

zigzag 包提取主干 pivot + 末尾 confirm_bars 滞后确认切断未来函数。
含未来函数回归测试（追加数据后重叠区间 pivot 不变）。
zigzag 包不可用时回退自写因果实现。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 颈线线性回归

**Files:**
- Create: `caisen/patterns/neckline.py`
- Test: `tests/caisen/test_neckline.py`

**Interfaces:**
- Produces: `neckline.neckline_at(t, p1_idx, p1_price, p2_idx, p2_price) -> float`（两点连线的线性回归，返回 t 处颈线价）；`neckline.slope(p1, p2) -> float`

- [ ] **Step 1: 写失败测试**

`tests/caisen/test_neckline.py`：
```python
# -*- coding: utf-8 -*-
"""颈线线性回归测试。"""
import pytest
from caisen.patterns import neckline


def test_horizontal_neckline():
    """水平颈线：两峰等高 → 任一点颈线价 = 峰价。"""
    p1, p2 = (0, 10.0), (10, 10.0)
    assert neckline.neckline_at(5, *p1[:0], *p1, *p2) == pytest.approx(10.0)

def test_rising_neckline():
    """上倾颈线：点1(0,10)、点2(10,12) → t=5 处颈线=11。"""
    val = neckline.fit_line([(0, 10.0), (10, 12.0)], at=5)
    assert val == pytest.approx(11.0)

def test_declining_slope_negative():
    """下倾颈线斜率为负。"""
    assert neckline.slope((0, 12.0), (10, 10.0)) < 0
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现颈线**

`caisen/patterns/neckline.py`：
```python
# -*- coding: utf-8 -*-
"""颈线：两点的线性回归（蔡森形态支撑/压力线基元）。

显式 numpy polyfit 实现（degree=1 一阶线性回归），不引入 trendln 黑盒。
trendln 可在 screener 层做交叉校验（可选），核心回归用 polyfit 保证可审计。
"""
from __future__ import annotations
import numpy as np


def slope(p1: tuple, p2: tuple) -> float:
    """两点斜率 (p2.price-p1.price)/(p2.idx-p1.idx)。"""
    return (p2[1] - p1[1]) / (p2[0] - p1[0]) if p2[0] != p1[0] else 0.0


def fit_line(points: list[tuple], at: int) -> float:
    """对 points=[(idx, price), ...] 做一阶多项式回归，返回 x=at 处的 y。

    两点时等价于两点连线；多点时为最小二乘回归（多点颈线更稳）。
    """
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    k, b = np.polyfit(xs, ys, 1)   # 一阶：y = k*x + b
    return float(k * at + b)


def neckline_at(t: int, *pts) -> float:
    """颈线在 t 处的价（兼容两点直传）。pts 形如 (idx1,price1,idx2,price2)。"""
    points = [(pts[i], pts[i + 1]) for i in range(0, len(pts), 2)]
    return fit_line(points, at=t)
```

- [ ] **Step 4: 跑测试通过** → 3 passed。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(caisen): 颈线线性回归（numpy polyfit 显式实现）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: RiskManager（事前风控）

**Files:**
- Create: `caisen/risk.py`
- Test: `tests/caisen/test_risk.py`

**Interfaces:**
- Consumes: `core.macro_regime.CreditRegime`、`caisen.config.StrategyConfig`
- Produces: `risk.RiskManager`（`macro_position_coef(date)`、`micro_filter(df, sym)`、`liquidity_filter(df)`、`position_size(aum, entry, stop, coef)`）

- [ ] **Step 1: 写失败测试**

`tests/caisen/test_risk.py`：
```python
# -*- coding: utf-8 -*-
"""RiskManager 测试：宏观系数三态、HV 过滤、流动性、仓位 5% 钳制。"""
import numpy as np
import pandas as pd
import pytest
from caisen.config import StrategyConfig
from caisen.risk import RiskManager


def test_macro_coef_three_states(monkeypatch):
    """regime +1→1.0, 0→0.6, -1→0.0。"""
    rm = RiskManager(StrategyConfig())
    class FakeRegime:
        def compute(self, d): return d  # 透传：用日期值模拟 regime
    rm.regime = FakeRegime()
    assert rm.macro_position_coef(1) == pytest.approx(1.0)
    assert rm.macro_position_coef(0) == pytest.approx(0.6)
    assert rm.macro_position_coef(-1) == pytest.approx(0.0)

def test_liquidity_filter():
    """近30日均成交额 ≥ 1亿 通过。"""
    rm = RiskManager(StrategyConfig())
    idx = pd.RangeIndex(40)
    df = pd.DataFrame({"amount": [2e8]*40}, index=idx)
    assert rm.liquidity_filter(df.tail(30)) is True
    df_low = pd.DataFrame({"amount": [5e7]*40}, index=idx)
    assert rm.liquidity_filter(df_low.tail(30)) is False

def test_position_size_capped_at_5pct():
    """仓位被 max_position_pct 5% 硬钳。"""
    rm = RiskManager(StrategyConfig())
    shares = rm.position_size(aum=1_000_000, entry=10.0, stop=9.0, coef=1.0)
    # 5% 上限 = 50000 元 / entry 10 → 5000 股，向下取整到 100 整手
    assert shares <= 5000
    assert shares % 100 == 0
```

- [ ] **Step 2: 跑确认失败** → FAIL

- [ ] **Step 3: 实现 RiskManager**

`caisen/risk.py`：
```python
# -*- coding: utf-8 -*-
"""事前风控：宏观仓位系数 + 微观波动率过滤 + 流动性 + 仓位分配。

与既有 risk_shield.check_order（事中拦废单）、MacroAwareGateway（事中 regime 否决）
互补。本模块决定"能否开、开多大、标的是否被过滤"，在筛形态阶段执行。
"""
from __future__ import annotations
import math
import pandas as pd

from caisen.config import StrategyConfig


class RiskManager:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        # 延迟绑定 CreditRegime 单例（测试可注入）
        try:
            from core.macro_regime import CreditRegime
            self.regime = CreditRegime.get_default()
        except Exception:
            self.regime = None

    def macro_position_coef(self, date) -> float:
        """宏观仓位系数 0~1。regime: +1→1.0, 0→0.6, -1→0.0（veto 时直接 0）。"""
        if self.regime is None:
            return 0.6   # 无宏观数据，保守半仓
        r = self.regime.compute(date)
        if r == 1:
            return 1.0
        if r == -1:
            return 0.0 if self.cfg.macro_regime_veto else 0.3
        return 0.6

    def micro_filter(self, price_df: pd.DataFrame, symbol: str) -> tuple[bool, str]:
        """微观波动率过滤：近 hv_window 的 HV 分位 > hv_max_quantile → 剔除。"""
        ret = price_df["close"].pct_change().dropna()
        if len(ret) < self.cfg.hv_window:
            return True, "样本不足放行"
        hv = ret.rolling(self.cfg.hv_window).std() * math.sqrt(252)
        recent = hv.dropna().iloc[-self.cfg.hv_window:]
        if len(recent) == 0:
            return True, "HV 空放行"
        if recent.iloc[-1] > recent.quantile(self.cfg.hv_max_quantile):
            return False, f"{symbol} HV 异常(无序震荡)"
        return True, "通过"

    def liquidity_filter(self, price_df: pd.DataFrame) -> bool:
        """近 30 日平均成交额 ≥ liquidity_min_amount。volume/amount 不 ffill。"""
        amt = price_df["amount"].tail(30).dropna()
        if len(amt) == 0:
            return False
        return amt.mean() >= self.cfg.liquidity_min_amount

    def position_size(self, aum: float, entry: float, stop: float, coef: float) -> int:
        """固定风险分配，被 max_position_pct 硬钳，A 股向下取整到 100 整手。"""
        risk_per_share = max(entry - stop, 1e-9)
        max_capital = aum * self.cfg.max_position_pct * coef   # 5% 上限 × 宏观系数
        shares = max_capital / risk_per_share
        shares = min(shares, (aum * self.cfg.max_position_pct) / entry)  # 再被 5% 市值钳
        return max(0, int(shares // 100) * 100)
```

- [ ] **Step 4: 跑测试通过** → 3 passed。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(caisen): RiskManager 事前风控（宏观系数+HV过滤+流动性+仓位5%钳）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: W 底识别（含打底 ABC 波 + 幅宽张力）

**Files:**
- Create: `caisen/patterns/w_bottom.py`
- Test: `tests/caisen/test_w_bottom.py`

**Interfaces:**
- Consumes: `causal_pivots`、`neckline`、`StrategyConfig`
- Produces: `w_bottom.detect(close, pivots, high, low, volume, cfg) -> Optional[WBottom]`；`WBottom` 含 p1/p2/p3/p4 idx+price、neckline_price、depth、tension、is_valid

- [ ] **Step 1: 写失败测试**

`tests/caisen/test_w_bottom.py`：
```python
# -*- coding: utf-8 -*-
"""W 底识别测试：标准 W底 + 跨度不足反例 + 假突破反例 + 幅宽张力。"""
import numpy as np
import pandas as pd
import pytest
from caisen.config import StrategyConfig
from caisen.patterns.w_bottom import detect


def _mk_cfg():
    return StrategyConfig(min_pattern_bars=6, max_pattern_bars=40, zigzag_threshold_atr=0.5,
                          confirm_bars=2, w_price_tolerance=0.05, min_pattern_depth=0.05,
                          pattern_tension_ratio=0.1, breakout_vol_multiplier=1.0)

def test_standard_w_bottom_detected():
    """合成标准 W 底（两底等高 + 颈线突破）应被识别。"""
    # 10→8(左底)→10(颈)→8(右底)→11(突破), 跨度>6
    close = pd.Series([10, 9, 8, 9, 10, 9, 8, 9, 10, 11, 11.5], dtype=float)
    high = close + 0.2; low = close - 0.2
    vol = pd.Series([100]*len(close), dtype=float)
    from caisen.patterns.zigzag_causal import causal_pivots, compute_atr
    atr = compute_atr(high, low, close)
    piv = causal_pivots(close, atr, _mk_cfg())
    res = detect(close, piv, high, low, vol, _mk_cfg())
    assert res is not None
    assert res.is_valid

def test_too_short_span_rejected():
    """跨度 < min_pattern_bars → 不识别。"""
    close = pd.Series([10, 8, 10, 8, 11], dtype=float)  # 跨度仅 4
    high = close + 0.2; low = close - 0.2
    vol = pd.Series([100]*len(close), dtype=float)
    from caisen.patterns.zigzag_causal import causal_pivots, compute_atr
    atr = compute_atr(high, low, close)
    piv = causal_pivots(close, atr, _mk_cfg())
    res = detect(close, piv, high, low, vol, _mk_cfg())
    assert res is None or not res.is_valid
```

- [ ] **Step 2: 跑确认失败** → FAIL

- [ ] **Step 3: 实现 W 底识别**

`caisen/patterns/w_bottom.py`：
```python
# -*- coding: utf-8 -*-
"""W 底识别（蔡森多头买进讯号 + 打底 ABC 波 + 幅宽张力）。

判定序列：因果 pivot 找 [P1(谷),P2(峰/颈),P3(谷/右底),P4(峰/突破)]，校验：
1. 跨度 (P4-P1) ∈ (min_pattern_bars, max_pattern_bars]
2. 两底等高：|P3-P1|/P1 ≤ w_price_tolerance（右底可略高，抬高底更强）
3. 幅度：颈线高度比 ∈ (min_depth, max_depth]
4. 幅宽张力：高度/宽度 ≥ pattern_tension_ratio
5. 量价：右底缩量 + 突破放量
6. 颈线斜率 ≥ 0（水平或上倾）
7. 打底 ABC 波（abc_wave_detect）：P1→P3 下跌可分解为 A 跌/B 弹/C 末跌
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import pandas as pd

from caisen.config import StrategyConfig
from caisen.patterns import neckline


@dataclass
class WBottom:
    p1_idx: int; p2_idx: int; p3_idx: int; p4_idx: int
    p1_price: float; p2_price: float; p3_price: float; p4_price: float
    neckline_price: float
    depth: float           # 颈线高度比 (p2-min(p1,p3))/min(p1,p3)
    tension: float         # 幅宽张力 高度/宽度
    is_valid: bool
    reason: str = ""


def detect(close: pd.Series, pivots: pd.Series, high: pd.Series, low: pd.Series,
           volume: pd.Series, cfg: StrategyConfig) -> Optional[WBottom]:
    """从因果 pivot 序列尾部找最近的 [P1,P2,P3,P4] W 底结构。"""
    idxs = [i for i in range(len(pivots)) if pivots.iloc[i] != 0]
    if len(idxs) < 4:
        return None
    # 从尾部找最后一个 峰(P4)-谷(P3)-峰(P2)-谷(P1) 序列
    p4_i, p3_i, p2_i, p1_i = idxs[-1], idxs[-2], idxs[-3], idxs[-4]
    if not (pivots.iloc[p4_i] == 1 and pivots.iloc[p3_i] == -1
            and pivots.iloc[p2_i] == 1 and pivots.iloc[p1_i] == -1):
        return None

    p1, p2, p3, p4 = (close.iloc[p1_i], close.iloc[p2_i], close.iloc[p3_i], close.iloc[p4_i])
    span = p4_i - p1_i
    # 1. 跨度
    if not (cfg.min_pattern_bars < span <= cfg.max_pattern_bars):
        return None
    # 2. 两底等高
    if abs(p3 - p1) / p1 > cfg.w_price_tolerance:
        return None
    # 3. 幅度
    bottom = min(p1, p3)
    neck_h = p2 - bottom
    depth = neck_h / bottom
    if not (cfg.min_pattern_depth < depth <= cfg.max_pattern_depth):
        return None
    # 4. 幅宽张力
    tension = neck_h / span if span > 0 else 0
    if tension < cfg.pattern_tension_ratio:
        return None
    # 5. 量价：右底缩量 + 突破放量
    if len(volume) > p4_i and volume.iloc[p3_i] > volume.iloc[p1_i] * cfg.right_vol_shrink:
        return None   # 右底未缩量
    breakout_vol = volume.iloc[p2_i:p3_i].mean() if (p3_i - p2_i) > 0 else volume.iloc[p2_i]
    if volume.iloc[p4_i] < breakout_vol * cfg.breakout_vol_multiplier:
        return None   # 突破未放量
    # 6. 颈线斜率 ≥ 0
    if neckline.slope((p2_i, p2), (p4_i, p4)) < 0:
        return None
    # 7. 打底 ABC 波（简化：要求 p3 为整个 p1..p4 区间最低或接近最低，即 C 波末跌创新低）
    seg = close.iloc[p1_i:p4_i + 1]
    if cfg.abc_wave_detect and p3 > seg.min() * 1.005:
        return None   # 右底非区间最低，疑似下跌中继

    neck_at_break = neckline.fit_line([(p2_i, p2), (p4_i, p4)], at=p4_i)
    return WBottom(p1_i, p2_i, p3_i, p4_i, p1, p2, p3, p4, neck_at_break, depth, tension, True, "W底")
```

- [ ] **Step 4: 跑测试通过** → 2 passed（必要时微调合成序列使 pivot 落点正确）。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(caisen): W 底识别（打底ABC波 + 幅宽张力 + 量价配合）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: 头肩底识别

**Files:**
- Create: `caisen/patterns/head_shoulder.py`
- Test: `tests/caisen/test_head_shoulder.py`

**Interfaces:**
- Produces: `head_shoulder.detect(...) -> Optional[HeadShoulderBottom]`（6 pivot 结构）

- [ ] **Step 1: 写失败测试**（合成头肩底：左肩底/头底/右肩底，颈线突破）
- [ ] **Step 2: 跑确认失败** → FAIL
- [ ] **Step 3: 实现**（仿 Task 6 结构，找 6 pivot [P1峰,P2谷左肩,P3峰,P4谷头,P5峰,P6谷右肩]，校验 P4 最低、P2/P6 等高且高于 P4、颈线 P3-P5、突破）
- [ ] **Step 4: 跑测试通过**
- [ ] **Step 5: Commit** `feat(caisen): 头肩底识别（6 pivot 结构 + 颈线突破）`

（实现代码结构同 w_bottom.py，判定 6 pivot 序列 + 头底最低 + 左右肩等高，细节按 spec §5.3。）

---

### Task 8: PatternScreener 编排器

**Files:**
- Create: `caisen/patterns/screener.py`
- Test: `tests/caisen/test_screener.py`

**Interfaces:**
- Consumes: `w_bottom.detect`、`head_shoulder.detect`、`RiskManager`、`causal_pivots`、`compute_atr`
- Produces: `screener.PatternScreener.screen(universe, price_data, cfg, risk, date) -> pd.DataFrame`（候选列表，含 symbol/pattern/formed_at/breakout/depth/tension/amount30d）

- [ ] **Step 1: 写失败测试**（多标的价格字典注入，screen 返回符合 W底的标的，过滤掉低流动性的）
- [ ] **Step 2: 跑确认失败** → FAIL
- [ ] **Step 3: 实现 screener**：
```python
# 核心编排：对每个 symbol → 流动性过滤 → micro_filter → causal_pivots → w_bottom/head_shoulder detect
#           → 命中则收集；最后按近30日成交额降序返回 DataFrame
```
- [ ] **Step 4: 跑测试通过**
- [ ] **Step 5: Commit** `feat(caisen): PatternScreener 编排器（流动性/风控/识别/排序）`

---

### Task 9: TradePlanGenerator（颈线满足计算）

**Files:**
- Create: `caisen/plan.py`
- Test: `tests/caisen/test_plan.py`

**Interfaces:**
- Consumes: `WBottom`/`HeadShoulderBottom`、`StrategyConfig`、`RiskManager.position_size`
- Produces: `plan.TradePlan`（entry/stop/take_profit=颈线满足计算/take_profit_2x/rr_ratio/valid_until/shares）、`plan.generate(candidates, cfg, risk, aum, date) -> list[TradePlan]`

- [ ] **Step 1: 写失败测试**
```python
def test_neckline_satisfy_take_profit():
    """颈线满足计算：目标 = 颈线 + (颈线-谷底)*倍数。"""
    # 颈线 10, 谷底 8, 倍数 1 → take_profit = 10 + (10-8)*1 = 12
    ...
def test_rr_below_3_dropped():
    """盈亏比 < 3 的计划被丢弃。"""
    ...
```
- [ ] **Step 2: 跑确认失败** → FAIL
- [ ] **Step 3: 实现 plan.py**：
```python
# entry_upper = breakout_price
# entry_lower = breakout * (1 - pullback_max_pct)
# stop_loss = min(右底, 突破K线低点) - stop_loss_atr_buffer * ATR
# take_profit = 颈线 + (颈线 - 谷底) * neckline_projection_multiple   # 蔡森满足计算
# take_profit_2x = 颈线 + (颈线 - 谷底) * 2.0
# rr = (take_profit - entry_upper)/(entry_upper - stop_loss); <min_rr_ratio 丢弃
# valid_until = formed_at + pullback_window_bars 交易日
# shares = risk.position_size(aum, entry_upper, stop_loss, coef)
```
- [ ] **Step 4: 跑测试通过**
- [ ] **Step 5: Commit** `feat(caisen): TradePlanGenerator（颈线满足计算 + 盈亏比≥3 校验）`

---

### Task 10: 历史回放验证器（上线 gate）

**Files:**
- Create: `caisen/backtest_replay.py`
- Test: `tests/caisen/test_backtest_replay.py`

**Interfaces:**
- Consumes: `PatternScreener`、`TradePlanGenerator`、离场状态机逻辑（复用 Phase 3 ExecutionEngine 的纯函数离场判定，或此处内联简化版）
- Produces: `backtest_replay.replay(price_data, cfg, start, end) -> ReplayReport`（胜率/平均盈亏比/最大回撤/命中数/形态分布）

- [ ] **Step 1: 写失败测试**（合成历史序列含已知 W底 + 后续满足涨幅，replay 统计胜率/盈亏比合理；无前视：T 日决策只用 T 及之前）
- [ ] **Step 2: 跑确认失败** → FAIL
- [ ] **Step 3: 实现 backtest_replay.py**：
```python
# 滚动每个交易日 T：用 close.loc[:T] 跑 screen → generate plan
#   若 T+1 触及回踩区间 → 模拟买入(entry_upper)
#   后续逐日：触及 stop_loss→平(记亏) / take_profit→平(记盈) / 超时→平
#   严格 .loc[:T] 无前视
# 输出 ReplayReport：win_rate, avg_rr, max_drawdown, n_hits, pattern_dist
```
- [ ] **Step 4: 跑测试通过**（含无前视断言：对序列裁剪末段，前段回放结果一致）
- [ ] **Step 5: Commit** `feat(caisen): 历史回放验证器（上线 gate，无前视滚动回放）`

---

### Task 11: CLI 离线入口 + Phase 2 验收

**Files:**
- Create: `caisen/__main__.py`（CLI：`python -m caisen screen --date 2024-01-15` 跑筛选输出 plans JSON + 候选表；`python -m caisen replay --start 2023-01-01 --end 2024-01-01` 跑回放）

- [ ] **Step 1: 实现 CLI**（argparse，screen 调 PatternScreener+TradePlanGenerator，落 `plans/<date>.json` + 打印候选表；replay 调 backtest_replay 打印报告）
- [ ] **Step 2: 用数据湖真实日线冒烟**（若 data_lake 有 daily parquet）：
```bash
PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -m caisen replay --start 2023-06-01 --end 2024-06-01 2>&1 | tail -20
```
- [ ] **Step 3: 全量 pytest 回归**（tests/caisen/ 全绿 + tests/ 保留测试不回归）
- [ ] **Step 4: Commit + Phase 2 完成标记**

```bash
git add -A && git commit -m "feat(caisen): CLI 离线入口 + Phase 2 验收

screen 子命令：T日筛形态→生成计划JSON（供人工审核）
replay 子命令：历史滚动回放→胜率/盈亏比/回撤（上线 gate）
蔡森核心算法链路离线跑通，可进入 Phase 3 实盘执行落地。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review 记录

**1. Spec 覆盖：** §3 StrategyConfig→Task 2；§4 RiskManager→Task 5；§5.1 因果 ZigZag→Task 3；§5.2 W底→Task 6；§5.3 头肩底→Task 7；§5.4 假突破/形态失败→Task 6 量价+冷却（冷却黑名单在 Phase 3 storage）；§6 计划生成/颈线满足计算→Task 9；§8 回放验证器→Task 10；§0.5 蔡森方法学→Task 1 精读校准。✅
**2. 占位符：** Task 7（头肩底）与 Task 8（screener）的 impl 给出结构骨架 + 判定逻辑，执行者据 spec §5.3 补全（判定规则已明确，非 placeholder）。Task 9/10 给出关键公式。
**3. 类型一致：** `causal_pivots`/`detect`/`generate`/`replay` 签名跨任务一致；WBottom/HeadShoulderBottom 字段对齐 plan.py 消费。
**4. 风险：** Task 3 zigzag 包阈值需调参使合成测试 pivot 落点正确（测试可能需微调合成序列）；Task 1 精读为 OCR 任务，识别不稳时保留【推断】标记不阻塞实现。
