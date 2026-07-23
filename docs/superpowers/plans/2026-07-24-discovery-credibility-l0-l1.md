# 参数发现引擎 · L0+L1 可信度闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 `discovery/` 包的 L0 数据快照冻结 + L1 嵌套 OOS（2025/2026 holdout）骨架，固化当前 param_iter 冠军的去偏 OOS 水平作为 L1 验收锚，解决探查发现的"数据快照漂移"。

**Architecture:** L0 `snapshot.py` 冻结 universe+行情 → sha256 指纹（修漂移）；L1 `split.py` 产 2025 inner / 2026 outer 二段 + embargo；`objective.py` 用"全历史跑 `scan_symbol` + 按 `signal_date` 分段"范式（复用探查脚本验证过的方法）做信息隔离评估；`store.py` SQLite 三表落库（复用 `backtest/tasks_db.py` WAL）；`judging.py` 分层裁判最小版（L0 可行域闸 + L1 calmar）；`cli.py` 串起全链。

**Tech Stack:** Python 3.10（`.venv310`）、pandas、pyarrow、sqlite3（WAL）、pytest。无新增重型依赖（optuna 留给 Plan 3）。回测内核 `strategies/neckline/` 零改动。

## Global Constraints

- **同源内核**：复用 `strategies/neckline/backtest.py::scan_symbol(sym_df, window, exec=None, id_cfg=None)` 与 `risk_metrics(pnls, dates, pos_cap=0.05, freq_cap=150)`，与 `scripts/param_iter.py` 的 115.8% 基线逐字可比（ADR8：回测内核零改动）。
- **零反向依赖**：`discovery/` 不依赖 `trading/`（不实盘下单），只读 `strategies/neckline/` 内核。
- **反魔法**：不引 vectorbt/qlib/backtrader；optuna 不在本 plan。
- **快照冻结**：同一 `snapshot_hash` 下 trial 才可比；数据湖增量不污染历史（ADR3）。
- **信息隔离**：inner（2025）评估与 outer（2026）评估物理隔离——outer 结果不反馈任何选择（spec §6.2）。
- **全中文注释**：所有新增代码像素级中文注释（CLAUDE.md）。
- **Plan 1 范围限定**：inner=2025 / outer=2026 holdout（**非** walk-forward）；4 折 walk-forward + 熊市 regime 覆盖推后续 plan（需扩 2020-2024 数据 + universe 决策）。
- **环境**：`.venv310/Scripts/python.exe`；数据 `data_lake/a_shares_daily.parquet`（2020-2026 tushare 落湖）。
- **诚实标注**：2026 非"纯 OOS"（冠军用 2025+2026 全段 score 选出，2026 参与了选择），报告须标注。

---

## File Structure

| 文件 | 职责 | 创建/修改 |
|---|---|---|
| `discovery/__init__.py` | 导出公开 API（freeze/holdout_split/evaluate_on_segment/...） | 创建 |
| `discovery/snapshot.py` | L0：加载 universe（参数化 start）+ 冻结 sha256 指纹 | 创建 |
| `discovery/split.py` | L1：2025/2026 二段切分 + embargo（纯函数） | 创建 |
| `discovery/objective.py` | 评估函数：全历史跑 scan_symbol + 按 signal_date 分段；inner_eval/outer_eval 信息隔离 | 创建 |
| `discovery/store.py` | SQLite 三表（trial/snapshot/search_run）CRUD，复用 tasks_db.py WAL 模式 | 创建 |
| `discovery/judging.py` | 分层裁判最小版：L0 可行域闸 + L1 calmar 排序 | 创建 |
| `discovery/cli.py` | `python -m discovery snapshot\|oos\|champions` | 创建 |
| `tests/discovery/__init__.py` | 测试包标记 | 创建 |
| `tests/discovery/conftest.py` | 共享 fixture（合成 sym_df / 冠军 params） | 创建 |
| `tests/discovery/test_snapshot.py` | 快照指纹可复现/变化 | 创建 |
| `tests/discovery/test_split.py` | 二段切分不重叠 + embargo | 创建 |
| `tests/discovery/test_objective.py` | 全历史跑+分段、信息隔离、复现探查锚 | 创建 |
| `tests/discovery/test_store.py` | SQLite 去重/并发写/stale 排除 | 创建 |
| `tests/discovery/test_judging.py` | L0 闸淘汰 + calmar 全序 | 创建 |
| `tests/discovery/test_cli_oos.py` | 集成：当前冠军 2026 去偏报告（L1 验收锚，slow） | 创建 |

**依赖顺序**：Task 1 (snapshot) → Task 2 (split) → Task 3 (objective) → Task 4 (store) → Task 5 (judging) → Task 6 (cli + 验收闸)。每 task 产出独立可测交付物。

---

## Task 1: L0 数据快照冻结（`discovery/snapshot.py`）

**Files:**
- Create: `discovery/__init__.py`、`discovery/snapshot.py`
- Create: `tests/discovery/__init__.py`、`tests/discovery/conftest.py`、`tests/discovery/test_snapshot.py`

**Interfaces:**
- Produces: `snapshot_hash(universe_count, date_range, lake_start, universe_def=...) -> str`（纯函数，sha256[:16]）；`load_universe(start="2025-01-01") -> dict[str, pd.DataFrame]`；`freeze(lake_start="2025-01-01") -> (universe, SnapshotMeta)`；`SnapshotMeta` dataclass（snapshot_hash/universe_def/universe_count/date_range/lake_start）。

- [ ] **Step 1: 建包骨架 + 写失败测试**

`discovery/__init__.py`（空包标记，Plan 1 末尾 Task 6 再填导出）:
```python
# -*- coding: utf-8 -*-
"""参数发现引擎（spec 2026-07-23-param-discovery-engine-design.md v1.3）。

Plan 1（L0+L1 可信度闭环）：快照冻结 + 2025/2026 holdout 嵌套 OOS + 分层裁判最小版。
"""
```

`tests/discovery/__init__.py`（空）。

`tests/discovery/conftest.py`（合成 fixture，避免单测读真实大 parquet）:
```python
# -*- coding: utf-8 -*-
"""discovery 测试共享 fixture。合成数据，不依赖真实 data_lake（快）。"""
import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def synth_sym_df():
    """合成单标的 OHLCV（250 根日线，2025 全年），供 scan_symbol 跑通。

    价格平稳上行 + 噪声，让颈线法有信号又不至于退化。列对齐 scan_symbol 期望。
    """
    idx = pd.bdate_range("2025-01-01", periods=250)
    rng = np.random.default_rng(42)
    close = 10.0 + np.cumsum(rng.normal(0.02, 0.3, 250))
    high = close * (1 + rng.uniform(0, 0.03, 250))
    low = close * (1 - rng.uniform(0, 0.03, 250))
    opn = close + rng.normal(0, 0.1, 250)
    return pd.DataFrame({
        "open": opn, "high": high, "low": low, "close": close,
        "volume": rng.integers(1e6, 1e7, 250),
        "amount": rng.integers(1e7, 1e8, 250),  # 千元单位，≥1e5=1亿
    }, index=idx)


@pytest.fixture
def champion_params():
    """当前 param_iter 冠军参数（state.json best，21 维）。供 oos 集成测试用。"""
    return {
        "window": 80, "min_touches": 2, "min_suppression": 0.5,
        "local_extrema_window": 5, "min_bottoms": 2, "breakout_vol_mult": 1.0,
        "min_rr": 1.0, "max_h_atr": 5.0, "stop_atr_mult": 1.0, "tp_h_mult": 2.5,
        "decay_tau": 60,
        "max_holding": 20, "max_wait": 3, "cooldown": 8, "buy_limit_atr_mult": 1.0,
        "tp1_h_mult": 0.5, "tp1_portion": 0.3, "cancel_thresh_mult": None,
        "trailing_grace": 0, "trailing_step": 0.05, "trailing_floor": 0.0,
    }
```

`tests/discovery/test_snapshot.py`:
```python
# -*- coding: utf-8 -*-
"""L0 快照冻结测试：纯函数优先（快），freeze 集成标 slow。"""
import pytest


def test_snapshot_hash_deterministic():
    """同输入 → 同 hash（可复现性基石，spec ADR3）。"""
    from discovery.snapshot import snapshot_hash
    h1 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    h2 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    assert h1 == h2


def test_snapshot_hash_differs_on_count():
    """universe 数量变 → hash 变（探查实证的 1334→1332 必须能检出）。"""
    from discovery.snapshot import snapshot_hash
    h1 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    h2 = snapshot_hash(1332, "2025-01-01~2026-07-23", "2025-01-01")
    assert h1 != h2


def test_snapshot_hash_differs_on_date_range():
    """日期范围变（数据湖增量落湖）→ hash 变。"""
    from discovery.snapshot import snapshot_hash
    h1 = snapshot_hash(1334, "2025-01-01~2026-07-23", "2025-01-01")
    h2 = snapshot_hash(1334, "2025-01-01~2026-07-30", "2025-01-01")
    assert h1 != h2


@pytest.mark.slow
def test_freeze_loads_real_universe():
    """集成：freeze 真实加载（~5s），返回非空 universe + 元数据。需 data_lake。"""
    from discovery.snapshot import freeze
    universe, meta = freeze()
    assert len(universe) > 100                       # 创板科创应有数百只
    assert meta.universe_count == len(universe)
    assert len(meta.snapshot_hash) == 16
    assert "2025" in meta.date_range
```

`tests/conftest.py`（项目根已有则追加，无则新建）需注册 `slow` marker，否则 pytest 警告。先检查:
```bash
grep -q "slow" tests/conftest.py 2>/dev/null || echo 'def pytest_configure(c): c.addinivalue_line("markers", "slow: 需真实 data_lake，慢")' >> tests/conftest.py
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_snapshot.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.snapshot'`）。

- [ ] **Step 3: 实现 `discovery/snapshot.py`**

```python
# -*- coding: utf-8 -*-
"""L0 数据快照冻结（spec §6.1 / §1.4 漂移实证）。

物理意图：探查（scripts/probe_champion_oos.py）发现两次跑 universe 1334→1332 只、
冠军 ann 漂 6%——data_lake 增量 + 流动性边界票浮动致"连复现自己都做不到"。本模块
冻结 universe + 日期范围 → sha256 指纹，同一指纹下所有 trial 可比，数据湖后续增量
不污染历史试验（spec ADR3）。

MVP（Plan 1）：universe = 创板科创 2025 截面（start 参数化），含 2025+2026 数据；
inner=2025 / outer=2026 holdout（非 walk-forward，4 折推后续 plan）。

与 scripts/param_iter.load_universe 同源（创板科创 + 近30日均成交额≥1e5 千元=1亿），
但 start 参数化（后续 plan 跑 2020-2024 时改 start）。
"""
import hashlib
import json
from dataclasses import dataclass

import pandas as pd

LAKE_PATH = "data_lake/a_shares_daily.parquet"
DEFAULT_UNIVERSE_DEF = "创板科创/2025截面/近30日均额≥1e5千元"


@dataclass
class SnapshotMeta:
    """快照元数据（落 SQLite snapshot 表）。"""
    snapshot_hash: str          # sha256[:16] 冻结指纹
    universe_def: str           # universe 定义描述
    universe_count: int         # 标的数
    date_range: str             # 实际数据日期范围 "start~end"
    lake_start: str             # 加载起始日（参数化用）


def is_target_board(sym):
    """创业板(300/301)+科创板(688/689)。与 param_iter.is_target_board 同源。"""
    code = sym.split(".")[0]
    return code.startswith(("300", "301", "688", "689"))


def snapshot_hash(universe_count, date_range, lake_start, universe_def=DEFAULT_UNIVERSE_DEF):
    """纯函数：快照指纹 sha256[:16]。同输入→同输出（可复现基石），不读文件故可快速单测。"""
    sig = json.dumps({
        "universe_def": universe_def,
        "universe_count": int(universe_count),
        "date_range": str(date_range),
        "lake_start": str(lake_start),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]


def load_universe(start="2025-01-01"):
    """加载创板科创 start 至今可交易标的 → {symbol: sym_df}。

    近30日均成交额≥1e5 千元（=1亿元）过滤流动性。sym_df 含 start 至今 OHLCV（含
    2026），供 objective 全历史跑 scan_symbol + 按 signal_date 分段（不硬切 df，
    避免 window/ATR 预热丢失——探查脚本验证过的范式）。
    """
    lake = pd.read_parquet(LAKE_PATH)
    lake = lake[lake.index.get_level_values("date") >= pd.Timestamp(start)]
    syms = lake.index.get_level_values("symbol").unique().tolist()
    # 近30日均成交额（千元）；按 symbol 取 tail(30) 均值，对齐 param_iter 口径
    amt = lake.groupby("symbol")["amount"].apply(lambda s: s.tail(30).mean() if len(s) > 0 else 0.0)
    tradable = [s for s in syms if is_target_board(s) and amt.get(s, 0.0) >= 1e5]
    universe = {}
    for s in tradable:
        try:
            universe[s] = lake.xs(s, level="symbol").sort_index()
        except Exception:
            continue
    return universe


def freeze(lake_start="2025-01-01"):
    """冻结一个快照：加载 universe + 算指纹 → (universe, SnapshotMeta)。

    universe 全量加载（start 至今），objective 再按 signal_date 切 inner/outer，
    而非此处切——保证 scan_symbol 有完整历史做 window/ATR 预热。
    """
    universe = load_universe(start=lake_start)
    dates = []
    for sym_df in universe.values():
        dates.extend(list(sym_df.index))
    if dates:
        dmin, dmax = min(dates), max(dates)
        date_range = f"{pd.Timestamp(dmin).date()}~{pd.Timestamp(dmax).date()}"
    else:
        date_range = "empty"
    meta = SnapshotMeta(
        snapshot_hash=snapshot_hash(len(universe), date_range, lake_start),
        universe_def=DEFAULT_UNIVERSE_DEF,
        universe_count=len(universe),
        date_range=date_range,
        lake_start=lake_start,
    )
    return universe, meta
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_snapshot.py -v -m "not slow"
```
Expected: 3 passed（纯函数测试），slow 集成跳过。

- [ ] **Step 5: Commit**

```bash
git add discovery/__init__.py discovery/snapshot.py tests/discovery/
git commit -m "feat(discovery): L0 数据快照冻结 sha256 指纹（修探查实证的漂移）

- snapshot_hash 纯函数：同输入同输出（可复现基石），count/date_range 变即变
- freeze 加载创板科创 universe（start 参数化）+ 算冻结指纹
- 解决 probe_champion_oos.py 实证的两次跑 ann 漂 6% / universe 1334→1332

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: L1 二段切分（`discovery/split.py`）

**Files:**
- Create: `discovery/split.py`、`tests/discovery/test_split.py`

**Interfaces:**
- Produces: `Segment(name, start, end)` dataclass（含 `covers(d) -> bool`）；`HoldoutSplit(inner, outer, embargo_days)` dataclass；`holdout_split(embargo_days=5) -> HoldoutSplit`。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_split.py`:
```python
# -*- coding: utf-8 -*-
"""L1 二段切分测试：2025/2026 不重叠 + embargo + Segment.covers。"""
from datetime import date


def test_inner_outer_no_overlap():
    """inner 2025 严格在 outer 2026 之前，无重叠（spec §3.3）。"""
    from discovery.split import holdout_split
    s = holdout_split()
    assert s.inner.end < s.outer.start
    assert s.inner.start == date(2025, 1, 1)
    assert s.outer.end == date(2026, 12, 31)


def test_segment_covers():
    """Segment.covers 边界判定（含端点）。"""
    from discovery.split import Segment
    seg = Segment("t", date(2025, 1, 1), date(2025, 12, 31))
    assert seg.covers(date(2025, 6, 15)) is True
    assert seg.covers(date(2025, 1, 1)) is True       # 含左端
    assert seg.covers(date(2026, 1, 1)) is False
    assert seg.covers(date(2024, 12, 31)) is False


def test_segment_covers_timestamp():
    """pandas Timestamp 也能判（scan_symbol 返回的 signal_date 类型）。"""
    import pandas as pd
    from discovery.split import Segment
    seg = Segment("t", date(2025, 1, 1), date(2025, 12, 31))
    assert seg.covers(pd.Timestamp("2025-06-15")) is True


def test_embargo_configurable():
    """embargo_days 可配（吸收 2025→2026 持仓跨越）。"""
    from discovery.split import holdout_split
    assert holdout_split(embargo_days=5).embargo_days == 5
    assert holdout_split(embargo_days=20).embargo_days == 20
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_split.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.split'`）。

- [ ] **Step 3: 实现 `discovery/split.py`**

```python
# -*- coding: utf-8 -*-
"""L1 嵌套验证切分（spec §3.3，Plan 1 简化版：2025/2026 holdout）。

Plan 1 不做 4 折 walk-forward（需 2020-2024 数据 + universe 时点决策，推后续 plan），
退化为二段 holdout：inner=2025（样本内诊断）/ outer=2026（OOS 去偏锚）。
embargo 吸收 2025→2026 边界的 trailing 持仓跨越（颈线法 trailing grace/max_holding
可达数日~20 日，2025 末信号持仓可能跨到 2026 初；embargo 让 outer 评估跳过这段，
防 inner 持仓污染 outer）。
"""
from dataclasses import dataclass
from datetime import date


@dataclass
class Segment:
    """一段日期区间（inner test / outer holdout）。"""
    name: str
    start: date
    end: date

    def covers(self, d):
        """d 是否落在本段（含端点）。d 可为 date 或 pandas Timestamp。"""
        dd = d.date() if hasattr(d, "date") and callable(getattr(d, "date", None)) else d
        return self.start <= dd <= self.end


@dataclass
class HoldoutSplit:
    """Plan 1 二段切分。"""
    inner: Segment          # 2025（样本内诊断）
    outer: Segment          # 2026（OOS 去偏锚，不反馈搜索）
    embargo_days: int       # inner→outer 边界 embargo（吸收持仓跨越）


def holdout_split(embargo_days=5):
    """Plan 1 二段切分：inner 2025 / outer 2026。

    embargo_days 默认 5（颈线法 2025 末信号 max_holding≤20，但跨年的多为短线回踩，
    5 日吸收绝大多数；后续可按 trailing 配置调）。objective 在分段时会用 outer 段
    起点向后让 embargo_days 天，跳过边界持仓。
    """
    return HoldoutSplit(
        inner=Segment("inner_2025", date(2025, 1, 1), date(2025, 12, 31)),
        outer=Segment("outer_2026", date(2026, 1, 1), date(2026, 12, 31)),
        embargo_days=embargo_days,
    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_split.py -v
```
Expected: 4 passed。

- [ ] **Step 5: Commit**

```bash
git add discovery/split.py tests/discovery/test_split.py
git commit -m "feat(discovery): L1 二段切分 2025/2026 holdout + embargo

- Segment.covers 支持 date/Timestamp（signal_date 判段用）
- embargo_days 吸收 inner→outer 持仓跨越（颈线法 trailing/max_holding）
- Plan 1 退化 holdout，4 折 walk-forward 推后续 plan

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: L1 评估函数（`discovery/objective.py`，信息隔离）

**Files:**
- Create: `discovery/objective.py`、`tests/discovery/test_objective.py`

**Interfaces:**
- Consumes: `scan_symbol(sym_df, window, exec=None, id_cfg=None)`、`risk_metrics(pnls, dates)`（既有，`strategies/neckline/`）；`Segment`/`HoldoutSplit`（Task 2）。
- Produces: `run_full_scan(params, universe) -> list[dict]`（all_filled，每条带 `avg_pnl_pct`/`signal_date`/`symbol`）；`segment_metrics(all_filled, segment, embargo_days=0) -> dict`；`metrics_of(pairs) -> dict`（含 `calmar=ann/max_dd`）；`evaluate(params, universe, split) -> {"inner":..., "outer":..., "n_total":...}`。

**关键设计**：复用探查脚本 `probe_champion_oos.py` 验证过的范式——**全历史跑 `scan_symbol` 一次，按 `signal_date` 分段**（不硬切 df，避免 window/ATR 预热丢失）；inner/outer 从同一客观 all_filled 派生，信息隔离体现在调用方（outer metrics 不反馈选择，judging 只用 inner 排序）。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_objective.py`:
```python
# -*- coding: utf-8 -*-
"""L1 评估函数测试：分段/embargo/calmar 用合成 filled（快）；run_full_scan 集成标 slow。"""
from datetime import date

import pandas as pd
import pytest


def test_metrics_of_calmar():
    """calmar = ann / max_dd（max_dd→0 时 ann>0 给 inf，否则 0）。"""
    from discovery.objective import metrics_of
    pairs = [(1.0, pd.Timestamp("2025-06-01")), (2.0, pd.Timestamp("2025-06-02")),
             (-0.5, pd.Timestamp("2025-06-03"))]
    m = metrics_of(pairs)
    assert m["n"] == 3
    assert "calmar" in m
    if m["max_dd"] > 1e-9:
        assert abs(m["calmar"] - m["ann"] / m["max_dd"]) < 1e-6


def test_metrics_of_empty():
    from discovery.objective import metrics_of
    m = metrics_of([])
    assert m["n"] == 0 and m["calmar"] == 0.0


def test_segment_metrics_splits_by_date():
    """合成 filled 跨 2025/2026，segment_metrics 按段过滤 signal_date。"""
    from discovery.objective import segment_metrics
    from discovery.split import holdout_split
    split = holdout_split()
    all_filled = [
        {"avg_pnl_pct": 1.0, "signal_date": pd.Timestamp("2025-06-01")},
        {"avg_pnl_pct": 2.0, "signal_date": pd.Timestamp("2025-12-30")},
        {"avg_pnl_pct": 3.0, "signal_date": pd.Timestamp("2026-02-01")},
        {"avg_pnl_pct": 4.0, "signal_date": pd.Timestamp("2026-06-01")},
    ]
    assert segment_metrics(all_filled, split.inner, embargo_days=0)["n"] == 2
    assert segment_metrics(all_filled, split.outer, embargo_days=0)["n"] == 2


def test_embargo_skips_outer_boundary():
    """embargo_days 跳过 outer 开头 N 天信号（吸收 inner→outer 持仓跨越，spec §3.3）。"""
    from discovery.objective import segment_metrics
    from discovery.split import holdout_split
    split = holdout_split(embargo_days=31)   # 跳过 2026-01 全月
    all_filled = [
        {"avg_pnl_pct": 1.0, "signal_date": pd.Timestamp("2026-01-15")},   # embargo 内，剔除
        {"avg_pnl_pct": 2.0, "signal_date": pd.Timestamp("2026-02-15")},   # embargo 后，保留
    ]
    assert segment_metrics(all_filled, split.outer, embargo_days=31)["n"] == 1


def test_evaluate_returns_inner_outer(champion_params, synth_sym_df):
    """evaluate 用合成 universe（1 只 synth 标的）跑通，返回 inner/outer 两段 dict。
    合成数据信号可能为 0——只验证结构，不验证 ann>0（真实验证在 slow 集成）。"""
    from discovery.objective import evaluate
    from discovery.split import holdout_split
    universe = {"300001.SZ": synth_sym_df}   # fixture 直接注入合成 df
    res = evaluate(champion_params, universe, holdout_split())
    assert set(res.keys()) >= {"inner", "outer", "n_total"}
    # embargo=0 时 inner+outer 笔数 = 全部（合成数据可能 0 笔，0==0+0 也满足）
    assert res["n_total"] == res["inner"]["n"] + res["outer"]["n"]


@pytest.mark.slow
def test_evaluate_champion_real(champion_params):
    """集成：当前冠军真实 evaluate（~3min），复现探查锚——2026 outer ann>0（探查实证 145-182%）。"""
    from discovery.objective import evaluate
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    universe, _ = freeze()
    res = evaluate(champion_params, universe, holdout_split())
    assert res["inner"]["n"] > 0
    assert res["outer"]["n"] > 0
    assert res["outer"]["ann"] > 0   # 复现探查：2026 未塌
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_objective.py -v -m "not slow"
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.objective'`）。

- [ ] **Step 3: 实现 `discovery/objective.py`**

```python
# -*- coding: utf-8 -*-
"""L1 评估函数（spec §6.2，Plan 1 简化：inner/outer 二段，无 walk-forward）。

核心范式（探查脚本 probe_champion_oos.py 验证过）：全历史跑 scan_symbol 一次 → 收集
all_filled → 按 signal_date 分段。不硬切 df（scan_symbol 用 sym_df.iloc[:i+1] 截至信号
历史识别，传完整 sym_df 才保 window/ATR 预热）；分段只发生在 signal_date 维度，无前视。

信息隔离（spec §6.2）：inner/outer 从同一客观 all_filled 派生（同 params 同 universe 的
全部信号），但 outer 的 metrics 不反馈任何选择——由调用方（judging/cli）保证只用 inner
排序、outer 只进报告。Plan 1 无搜索，"隔离"主要约束 outer 不进冠军排序。
"""
import pandas as pd

from strategies.neckline.method_v0 import DEFAULTS
from strategies.neckline.backtest import scan_symbol, risk_metrics, EXEC_DEFAULTS

# 21 维参数分层键名（与 scripts/param_iter.PARAM_SPACE 同源；Plan 1 只需键名分层，不需候选值）
ID_KEYS = ["window", "min_touches", "min_suppression", "local_extrema_window",
           "min_bottoms", "breakout_vol_mult", "min_rr", "max_h_atr",
           "stop_atr_mult", "tp_h_mult", "decay_tau"]
EXEC_KEYS = ["max_holding", "max_wait", "cooldown", "buy_limit_atr_mult",
             "tp1_h_mult", "tp1_portion", "cancel_thresh_mult",
             "trailing_grace", "trailing_step", "trailing_floor"]


def run_full_scan(params, universe):
    """全历史跑一组参数 → all_filled（每条带 avg_pnl_pct/signal_date/symbol）。

    显式构造 id_cfg/exec_cfg 传入 scan_symbol（与 param_iter.run_one 同款，去全局 mutation）。
    遍历 universe 调 scan_symbol——单标的用 sym_df 全历史，保证 window/ATR 预热完整。
    """
    id_cfg = {**DEFAULTS, **{k: params[k] for k in ID_KEYS}}
    exec_cfg = {**EXEC_DEFAULTS, **{k: params[k] for k in EXEC_KEYS}}
    window = id_cfg["window"]
    all_filled = []
    for sym, sym_df in universe.items():
        try:
            filled, _n_sig, _n_skip = scan_symbol(sym_df, window, exec=exec_cfg, id_cfg=id_cfg)
            for r in filled:
                r["symbol"] = sym
            all_filled.extend(filled)
        except Exception:
            continue
    return all_filled


def metrics_of(pairs):
    """[(pnl, date), ...] → 风险指标 dict（含 calmar=ann/max_dd，分层裁判 L1 主目标）。"""
    if not pairs:
        return dict(n=0, ann=0.0, sharpe=0.0, max_dd=0.0, kelly=0.0, calmar=0.0, curve=1.0)
    pnls = [p for p, _ in pairs]
    dates = [d for _, d in pairs]
    kelly, curve, ann, sharpe, max_dd = risk_metrics(pnls, dates)
    # calmar = ann/max_dd；max_dd 极小时 ann>0 给 inf（极佳），ann≤0 给 0
    if max_dd > 1e-9:
        calmar = ann / max_dd
    else:
        calmar = float("inf") if ann > 0 else 0.0
    return dict(n=len(pnls), ann=ann, sharpe=sharpe, max_dd=max_dd, kelly=kelly,
                calmar=calmar, curve=curve)


def segment_metrics(all_filled, segment, embargo_days=0):
    """从 all_filled 按 signal_date 过滤到 segment（embargo 偏移）→ metrics dict。

    embargo：segment.start + embargo_days 天内的 signal_date 不计入（吸收前段持仓
    跨越——颈线法 trailing grace/max_holding 可达数日~20 日，inner 末信号持仓可能跨到
    outer 初；embargo 让 outer 评估跳过这段，防 inner 持仓污染 outer 信号统计）。
    """
    from datetime import timedelta
    embargo_cutoff = segment.start + timedelta(days=embargo_days)
    pairs = []
    for r in all_filled:
        d = pd.to_datetime(r["signal_date"])
        if embargo_days > 0 and d.date() < embargo_cutoff:
            continue
        if segment.covers(d):
            pairs.append((r["avg_pnl_pct"], d))
    return metrics_of(pairs)


def evaluate(params, universe, split):
    """评估给定 params 的 inner/outer 两段。

    跑一次 run_full_scan（同 params 同 universe 的客观信号），分 inner(2025)/outer(2026)
    两段。信息隔离：返回的 outer metrics 仅供报告，调用方不得用于冠军排序/选择（spec §6.2）。
    """
    all_filled = run_full_scan(params, universe)
    return {
        "inner": segment_metrics(all_filled, split.inner, embargo_days=0),
        "outer": segment_metrics(all_filled, split.outer, embargo_days=split.embargo_days),
        "n_total": len(all_filled),
    }
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_objective.py -v -m "not slow"
```
Expected: 5 passed（合成数据测试），slow 集成跳过。

- [ ] **Step 5: Commit**

```bash
git add discovery/objective.py tests/discovery/test_objective.py
git commit -m "feat(discovery): L1 评估函数 全历史跑+signal_date分段（信息隔离）

- run_full_scan 复用探查范式：scan_symbol 全历史，不硬切 df（保 window/ATR 预热）
- segment_metrics 按 signal_date 过滤 + embargo 吸收 inner→outer 持仓跨越
- metrics_of 加 calmar=ann/max_dd（分层裁判 L1 主目标，spec §3.5 v1.2）
- evaluate 返回 inner/outer，outer 仅供报告不反馈选择

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: SQLite 三表落库（`discovery/store.py`，复用 WAL 模式）

**Files:**
- Create: `discovery/store.py`、`tests/discovery/test_store.py`

**Interfaces:**
- Consumes: `SnapshotMeta`（Task 1）。
- Produces: `connect(db_path)` 上下文管理器（WAL + Row 工厂 + 单点写锁）；`init_db(db_path)`；`trial_id_of(params, snapshot_hash, seed) -> str`（sha256[:12] 去重键）；`write_snapshot(conn, meta)`；`write_trial(conn, trial_id, params, snapshot_hash, engine_hash, split, inner_metrics, outer_metrics, source)`；`trial_exists(conn, trial_id) -> bool`。

**关键设计**：对齐 `backtest/tasks_db.py` 的 WAL + Row 工厂 + 单点写模式（spec ADR6），不重造。schema 按 spec §3.4（Plan 1 精简：trial 表 inner/outer metrics 直接存 JSON，score_fn_version/seed 等留后续 plan）。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_store.py`:
```python
# -*- coding: utf-8 -*-
"""SQLite 三表测试：去重/落库/读取（tmp_path 隔离，不碰真实 db）。"""
import threading


def test_trial_id_deterministic():
    """同 params+snapshot+seed → 同 trial_id（去重键，spec §3.2）。"""
    from discovery.store import trial_id_of
    p = {"window": 80, "x": 1}
    assert trial_id_of(p, "snap1", 42) == trial_id_of(p, "snap1", 42)


def test_trial_id_differs_on_params():
    from discovery.store import trial_id_of
    assert trial_id_of({"window": 80}, "snap1", 42) != trial_id_of({"window": 60}, "snap1", 42)


def test_write_and_read_trial(tmp_path):
    """落 trial + 去重（同 trial_id 不重复写）。"""
    from discovery.store import init_db, connect, write_trial, trial_exists
    db = str(tmp_path / "t.db")
    init_db(db)
    with connect(db) as conn:
        write_trial(conn, "tid1", {"window": 80}, "snap1", "eng1", "holdout_2025_2026",
                    {"ann": 0.7}, {"ann": 1.8}, "manual")
        assert trial_exists(conn, "tid1") is True
        assert trial_exists(conn, "tid_other") is False


def test_concurrent_write_no_deadlock(tmp_path):
    """多线程并发写（WAL + 单点写锁）不锁死——spec §8 拷问②。"""
    from discovery.store import init_db, connect, write_trial
    db = str(tmp_path / "t.db")
    init_db(db)
    errors = []

    def writer(i):
        try:
            with connect(db) as conn:
                write_trial(conn, f"tid{i}", {"window": 80}, "snap1", "eng1",
                            "s", {"ann": 0.1}, {"ann": 0.2}, "t")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM trial").fetchone()["c"]
    assert n == 10


def test_write_snapshot(tmp_path):
    from discovery.store import init_db, connect, write_snapshot
    from discovery.snapshot import SnapshotMeta
    db = str(tmp_path / "t.db")
    init_db(db)
    meta = SnapshotMeta("snap1", "创板科创", 1334, "2025~2026", "2025-01-01")
    with connect(db) as conn:
        write_snapshot(conn, meta)
        row = conn.execute("SELECT * FROM snapshot WHERE snapshot_hash=?", ("snap1",)).fetchone()
    assert row["universe_count"] == 1334
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_store.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.store'`）。

- [ ] **Step 3: 实现 `discovery/store.py`**

```python
# -*- coding: utf-8 -*-
"""SQLite 三表落库（spec §3.4，对齐 backtest/tasks_db.py 的 WAL + 单点写模式，ADR6）。

三表：snapshot（快照登记）/ trial（单次试验）/ search_run（跑批）。Plan 1 精简：
trial 表直接存 inner/outer metrics JSON；score_fn_version/seed/oos_metrics 等留后续 plan。
WAL 模式 + threading.Lock 单点写，防多进程/多线程跨进程锁（spec §8 拷问②）。
"""
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager

DEFAULT_DB_PATH = "logs/discovery_trials.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
  snapshot_hash   TEXT PRIMARY KEY,
  universe_def    TEXT NOT NULL,
  universe_count  INTEGER,
  date_range      TEXT NOT NULL,
  lake_start      TEXT,
  created_at      TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trial (
  trial_id        TEXT PRIMARY KEY,
  params          TEXT NOT NULL,
  snapshot_hash   TEXT NOT NULL,
  engine_hash     TEXT NOT NULL,
  split           TEXT NOT NULL,
  inner_metrics   TEXT,
  outer_metrics   TEXT,
  source          TEXT NOT NULL,
  created_at      TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_trial_snapshot ON trial(snapshot_hash);
CREATE TABLE IF NOT EXISTS search_run (
  run_id          TEXT PRIMARY KEY,
  snapshot_hash   TEXT NOT NULL,
  started_at      TEXT,
  ended_at        TEXT,
  n_trials        INTEGER,
  status          TEXT,
  note            TEXT);
"""

_write_lock = threading.Lock()   # 单点写：跨线程串行化写，配合 WAL 防锁死


@contextmanager
def connect(db_path=DEFAULT_DB_PATH):
    """连接上下文：WAL + Row 工厂 + commit/close。写操作外加 _write_lock（调用方用 with connect）。"""
    import os
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)   # timeout 防 SQLITE_BUSY 短暂等待
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path=DEFAULT_DB_PATH):
    """建表（幂等）。"""
    with _write_lock, connect(db_path) as conn:
        conn.executescript(SCHEMA)


def trial_id_of(params, snapshot_hash, seed=0):
    """trial_id = sha256(params+snapshot+seed)[:12]，天然去重键（spec §3.2）。"""
    sig = json.dumps({"p": params, "s": snapshot_hash, "seed": seed},
                     sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:12]


def write_snapshot(conn, meta):
    """落 snapshot 表（upsert）。meta: SnapshotMeta。"""
    from datetime import datetime
    conn.execute(
        "INSERT OR REPLACE INTO snapshot "
        "(snapshot_hash, universe_def, universe_count, date_range, lake_start, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (meta.snapshot_hash, meta.universe_def, meta.universe_count,
         meta.date_range, meta.lake_start, datetime.utcnow().isoformat()))


def write_trial(conn, trial_id, params, snapshot_hash, engine_hash, split,
                inner_metrics, outer_metrics, source):
    """落 trial 表（INSERT OR IGNORE 去重——同 trial_id 不覆盖）。metrics 存 JSON。"""
    from datetime import datetime
    conn.execute(
        "INSERT OR IGNORE INTO trial "
        "(trial_id, params, snapshot_hash, engine_hash, split, inner_metrics, outer_metrics, source, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (trial_id, json.dumps(params, ensure_ascii=False, default=str), snapshot_hash,
         engine_hash, split, json.dumps(inner_metrics, ensure_ascii=False, default=str),
         json.dumps(outer_metrics, ensure_ascii=False, default=str), source,
         datetime.utcnow().isoformat()))


def trial_exists(conn, trial_id):
    """trial_id 是否已存在（断点续跑/去重用）。"""
    return conn.execute("SELECT 1 FROM trial WHERE trial_id=?", (trial_id,)).fetchone() is not None
```

注意：`test_concurrent_write_no_deadlock` 的并发写——`connect` 内部不加 `_write_lock`（锁在 `init_db`/外部），多线程各自 `connect` 会各自 commit。SQLite WAL 支持并发读 + 串行写，`timeout=30` + `INSERT OR IGNORE` 保证最终一致。若测试偶发 BUSY，在 `write_trial` 外包 `_write_lock`——作为 Step 4 后的调优（见 Step 4 备注）。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_store.py -v
```
Expected: 5 passed。若 `test_concurrent_write_no_deadlock` 偶发 `database is locked`，把 `write_trial` 调用点改为 `with _write_lock: write_trial(...)`（在 cli 集成层 Task 6 处理，单测内可直接在 connect 外加锁）。

- [ ] **Step 5: Commit**

```bash
git add discovery/store.py tests/discovery/test_store.py
git commit -m "feat(discovery): SQLite 三表落库 WAL+单点写（对齐 tasks_db.py）

- trial_id=sha256(params+snapshot+seed)[:12] 去重键
- snapshot/trial/search_run 三表（spec §3.4，Plan 1 精简）
- WAL + threading.Lock 单点写防跨进程锁（spec §8 拷问②）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: 分层裁判最小版（`discovery/judging.py`）

**Files:**
- Create: `discovery/judging.py`、`tests/discovery/test_judging.py`

**Interfaces:**
- Consumes: metrics dict（Task 3 `metrics_of`/`evaluate` 产出，含 `max_dd`/`n`/`calmar`）。
- Produces: `feasibility_gate(metrics, max_dd_max=0.4, n_min=30) -> bool`（L0 可行域闸）；`calmar_rank(candidates) -> list`（L1 主目标降序）。

**范围说明**：spec §3.5 v1.2 分层裁判四层，Plan 1 只做 L0 闸 + L1 calmar 全序。**L2 DSR + L3 邻域留后续 plan**（DSR 需 top-N 候选，Plan 1 无搜索；邻域稳定性在 Task 7 作手动验收，不进排序）。**熊市 ann≥0 在 Plan 1 标 N/A**（2025-2026 无熊市数据）。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_judging.py`:
```python
# -*- coding: utf-8 -*-
"""分层裁判最小版测试：L0 闸淘汰 + L1 calmar 全序。"""


def test_feasibility_gate_rejects_high_dd():
    """max_dd > 0.4 → 闸外淘汰（防极端回撤冠军，spec §3.5 L0）。"""
    from discovery.judging import feasibility_gate
    assert feasibility_gate({"max_dd": 0.5, "n": 100}) is False
    assert feasibility_gate({"max_dd": 0.4, "n": 100}) is True
    assert feasibility_gate({"max_dd": 0.3, "n": 100}) is True


def test_feasibility_gate_rejects_low_n():
    """n < 30 → 闸外淘汰（防统计意义不足的冠军）。"""
    from discovery.judging import feasibility_gate
    assert feasibility_gate({"max_dd": 0.2, "n": 10}) is False
    assert feasibility_gate({"max_dd": 0.2, "n": 30}) is True


def test_calmar_rank_filters_then_sorts():
    """先 L0 闸过滤，再按 calmar 降序全序。"""
    from discovery.judging import calmar_rank
    cands = [
        {"max_dd": 0.2, "n": 100, "calmar": 3.0},
        {"max_dd": 0.1, "n": 100, "calmar": 5.0},
        {"max_dd": 0.6, "n": 100, "calmar": 99.0},   # 闸外（max_dd>0.4）→ 剔除
        {"max_dd": 0.2, "n": 5, "calmar": 7.0},       # 闸外（n<30）→ 剔除
    ]
    ranked = calmar_rank(cands)
    assert len(ranked) == 2
    assert [c["calmar"] for c in ranked] == [5.0, 3.0]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_judging.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.judging'`）。

- [ ] **Step 3: 实现 `discovery/judging.py`**

```python
# -*- coding: utf-8 -*-
"""分层裁判最小版（spec §3.5 v1.2，Plan 1 只做 L0 闸 + L1 calmar）。

L0 可行域闸：max_dd≤0.4 ∧ n≥30。熊市 ann≥0 在 Plan 1 标 N/A——2025-2026 无熊市数据，
熊市一票否决待后续 plan 扩 regime 数据（spec §3.3 硬约束 + §1.4 真风险 regime 依赖）。
L1 主目标：可行域内按 calmar=ann/max_dd 降序全序（颈线法主风险是回撤，calmar 比 sharpe 贴）。
L2 DSR / L3 邻域留后续 plan（Plan 1 邻域稳定性在 Task 7 手动验收，不进排序）。
"""
FEASIBILITY_MAX_DD = 0.4    # spec §3.5 L0：回撤上限
FEASIBILITY_MIN_N = 30      # spec §3.5 L0：最小交易笔数（统计意义）


def feasibility_gate(metrics, max_dd_max=FEASIBILITY_MAX_DD, n_min=FEASIBILITY_MIN_N):
    """L0 可行域闸：max_dd≤阈值 ∧ n≥阈值（熊市项 Plan 1 N/A）。"""
    return metrics["max_dd"] <= max_dd_max and metrics["n"] >= n_min


def calmar_rank(candidates):
    """L1 主目标排序：可行域内按 calmar 降序全序。candidates = list[metrics dict]。"""
    feasible = [c for c in candidates if feasibility_gate(c)]
    return sorted(feasible, key=lambda c: c.get("calmar", 0.0), reverse=True)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_judging.py -v
```
Expected: 3 passed。

- [ ] **Step 5: Commit**

```bash
git add discovery/judging.py tests/discovery/test_judging.py
git commit -m "feat(discovery): 分层裁判最小版 L0闸+L1 calmar（spec §3.5 v1.2）

- feasibility_gate：max_dd≤0.4 ∧ n≥30（熊市项 Plan 1 N/A 无数据）
- calmar_rank：可行域内按 calmar=ann/max_dd 降序
- L2 DSR / L3 邻域留后续 plan

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: CLI 集成（`discovery/cli.py` + `discovery/__main__.py`）

**Files:**
- Create: `discovery/cli.py`、`discovery/__main__.py`
- Create: `tests/discovery/test_cli_oos.py`（集成，slow）

**Interfaces:**
- Consumes: Task 1-5 全部（freeze/holdout_split/evaluate/init_db/write_snapshot/write_trial/trial_id_of/feasibility_gate）。
- Produces: `python -m discovery oos [--embargo N]`——对当前 param_iter 冠军跑 2025/2026 holdout，打印去偏报告 + 落 SQLite。

- [ ] **Step 1: 写集成测试（slow，跑真实 oos 命令）**

`tests/discovery/test_cli_oos.py`:
```python
# -*- coding: utf-8 -*-
"""cli oos 集成测试：subprocess 跑 python -m discovery oos，验证 exit 0 + 报告含关键行。"""
import subprocess
import sys
import pytest


@pytest.mark.slow
def test_oos_command_produces_report(tmp_path, monkeypatch):
    """跑 discovery oos（~3min），exit 0，stdout 含 outer 2026 + 落库 trial_id。
    用 tmp_path 隔离 db，避免污染 logs/discovery_trials.db。"""
    db = tmp_path / "t.db"
    env = {"DISCOVERY_DB": str(db)}  # cli 读取环境变量覆盖 DEFAULT_DB_PATH（见 Step 3）
    proc = subprocess.run(
        [sys.executable, "-m", "discovery", "oos", "--embargo", "5"],
        capture_output=True, text=True, env={**__import__("os").environ, **env},
        cwd=os_cwd(),   # repo root（test 从 repo root 跑）
    )
    assert proc.returncode == 0, proc.stderr
    assert "★outer 2026" in proc.stdout
    assert "trial_id=" in proc.stdout
    assert "snapshot:" in proc.stdout


def os_cwd():
    """测试 cwd = repo root（pytest 从 repo root 跑）。"""
    import os
    return os.getcwd()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_cli_oos.py -v -m "not slow"
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.cli'`；slow 跳过）。

- [ ] **Step 3: 实现 `discovery/cli.py` + `discovery/__main__.py`**

`discovery/cli.py`:
```python
# -*- coding: utf-8 -*-
"""命令行：python -m discovery {oos,verify}。

oos：对当前 param_iter 冠军（logs/param_iter_state.json 的 best）跑 2025/2026 holdout
嵌套评估，固化其 2026 去偏水平（L1 验收锚），落 SQLite。解决探查实证的快照漂移。
"""
import argparse
import hashlib
import json
import os

from discovery.snapshot import freeze
from discovery.split import holdout_split
from discovery.objective import evaluate
from discovery.store import (init_db, connect, write_snapshot, write_trial,
                             trial_id_of, DEFAULT_DB_PATH)
from discovery.judging import feasibility_gate

STATE_FILE = "logs/param_iter_state.json"


def _db_path():
    """环境变量 DISCOVERY_DB 覆盖（测试隔离用）。"""
    return os.environ.get("DISCOVERY_DB", DEFAULT_DB_PATH)


def _engine_hash():
    """回测内核代码 hash（backtest.py+method_v0.py 内容 sha256[:12]）。
    内核改了老 trial 标 stale 的锚（spec §3.2 engine_hash）。"""
    from strategies.neckline import backtest, method_v0
    h = hashlib.sha256()
    for f in (backtest.__file__, method_v0.__file__):
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


def cmd_oos(args):
    """当前冠军 2026 去偏评估 + 落库。"""
    universe, meta = freeze()
    split = holdout_split(args.embargo)
    state = json.load(open(STATE_FILE, encoding="utf-8"))
    params = state["best"]
    print(f"=== discovery oos：当前冠军 2026 去偏（snapshot={meta.snapshot_hash}）===")
    print(f"snapshot: {meta.universe_count} 只 | {meta.date_range} | hash {meta.snapshot_hash}")
    res = evaluate(params, universe, split)
    _print_segment("inner 2025", res["inner"])
    _print_segment("★outer 2026", res["outer"])
    print(f"L0 可行域闸(inner): {'通过' if feasibility_gate(res['inner']) else '不通过'} "
          f"(熊市 ann≥0 在 Plan 1 N/A——2025-2026 无熊市数据)")
    print(f"诚实标注: 2026 非纯 OOS（冠军用 2025+2026 全段 score 选出，2026 参与了选择）；"
          f"夏普/ann 是 risk_metrics 复利放大产物，绝对值非实盘预期")
    db = _db_path()
    init_db(db)
    eng = _engine_hash()
    tid = trial_id_of(params, meta.snapshot_hash)
    with connect(db) as conn:
        write_snapshot(conn, meta)
        write_trial(conn, tid, params, meta.snapshot_hash, eng, "holdout_2025_2026",
                    res["inner"], res["outer"], "manual_champion")
    print(f"落库: db={db} trial_id={tid} engine_hash={eng}")


def _print_segment(name, m):
    print(f"{name:>12}: ann {m['ann']*100:>6.1f}%  calmar {m['calmar']:>5.2f}  "
          f"夏普{m['sharpe']:>5.2f}  回撤{m['max_dd']*100:>5.1f}%  {m['n']:>5}笔")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="discovery")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_oos = sub.add_parser("oos", help="当前冠军 2026 去偏评估（L1 验收锚）")
    ap_oos.add_argument("--embargo", type=int, default=5, help="inner→outer embargo 天数")
    ap_oos.set_defaults(func=cmd_oos)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
```

`discovery/__main__.py`（让 `python -m discovery` 生效）:
```python
# -*- coding: utf-8 -*-
"""python -m discovery 入口。"""
from discovery.cli import main

if __name__ == "__main__":
    main()
```

`discovery/__init__.py` 更新导出（Task 1 留空，现填）:
```python
# -*- coding: utf-8 -*-
"""参数发现引擎（spec 2026-07-23-param-discovery-engine-design.md v1.3）。

Plan 1（L0+L1 可信度闭环）：快照冻结 + 2025/2026 holdout 嵌套 OOS + 分层裁判最小版。
"""
from discovery.snapshot import freeze, SnapshotMeta, snapshot_hash
from discovery.split import holdout_split, Segment, HoldoutSplit
from discovery.objective import evaluate, run_full_scan, segment_metrics, metrics_of
from discovery.judging import feasibility_gate, calmar_rank

__all__ = ["freeze", "SnapshotMeta", "snapshot_hash", "holdout_split", "Segment",
           "HoldoutSplit", "evaluate", "run_full_scan", "segment_metrics", "metrics_of",
           "feasibility_gate", "calmar_rank"]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_cli_oos.py -v -m "not slow" -k "not slow"
.venv310/Scripts/python.exe -m discovery oos --embargo 5   # 手动冒烟（~3min）
```
Expected: 非slow 测试通过；手动 oos 打印 inner/outer 报告 + 落库 trial_id，outer 2026 ann 复现探查锚（>0，预期 145-182% 区间）。

- [ ] **Step 5: Commit**

```bash
git add discovery/cli.py discovery/__main__.py discovery/__init__.py tests/discovery/test_cli_oos.py
git commit -m "feat(discovery): cli oos 命令 串起全链（L1 验收锚固化）

- python -m discovery oos：freeze+evaluate+judging+store，对当前冠军产 2026 去偏报告
- _engine_hash 回测内核指纹（老 trial stale 锚）
- DISCOVERY_DB 环境变量（测试隔离）
- 诚实标注 2026 非纯 OOS + 夏普/ann 复利放大

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: 邻域稳定性验收闸（`discovery/neighborhood.py` + `verify` 命令）

**Files:**
- Create: `discovery/neighborhood.py`、`tests/discovery/test_neighborhood.py`
- Modify: `discovery/cli.py`（加 `verify` 子命令）

**Interfaces:**
- Consumes: `evaluate`（Task 3）；`freeze`/`holdout_split`。
- Produces: `perturb_params(params, perturb, rng, n_dims=3) -> dict`；`neighborhood_stability(params, universe, split, perturb=0.15, n_samples=5, seed=42) -> dict`（含 `base_calmar`/`neighbor_mean`/`std`/`is_plateau`）；cli `verify` 命令。

**物理意图**（spec §12⑥ / §3.5 L3）：冠军在 21 维邻域 ±扰动下 calmar 稳定 = 高原（稳健，放行）；塌 = 孤峰（过拟合尖峰，否决）。Plan 1 手动验收（不进排序，排序留 Plan 3 搜索）。用百分比扰动（不依赖 PARAM_SPACE 候选档，discovery 自洽）。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_neighborhood.py`:
```python
# -*- coding: utf-8 -*-
"""邻域稳定性测试：perturb_params 结构 + neighborhood_stability 字段（合成 universe 慢，标 slow 边界）。"""
import random


def test_perturb_params_keeps_none():
    """None 参数（cancel_thresh_mult/decay_tau）不扰动，保留原值。"""
    from discovery.neighborhood import perturb_params
    p = {"window": 80, "decay_tau": None, "cancel_thresh_mult": None}
    rng = random.Random(0)
    nb = perturb_params(p, 0.15, rng, n_dims=1)
    assert nb["decay_tau"] is None
    assert nb["cancel_thresh_mult"] is None
    assert isinstance(nb["window"], (int, float))


def test_perturb_params_changes_numeric():
    from discovery.neighborhood import perturb_params
    p = {"window": 80, "stop_atr_mult": 1.0}
    rng = random.Random(0)
    nb = perturb_params(p, 0.5, rng, n_dims=2)
    # 至少一个数值参数被改（×[0.5,1.5]）
    changed = any(nb[k] != p[k] for k in p)
    assert changed


def test_neighborhood_stability_fields(champion_params, synth_sym_df):
    """合成 universe 跑 neighborhood_stability，验证返回字段齐全（ann 值不验证，合成数据）。"""
    from discovery.neighborhood import neighborhood_stability
    from discovery.split import holdout_split
    universe = {"300001.SZ": synth_sym_df}
    stab = neighborhood_stability(champion_params, universe, holdout_split(),
                                  perturb=0.2, n_samples=3)
    assert set(stab.keys()) >= {"base_calmar", "neighbor_calmars", "neighbor_mean",
                                "std", "is_plateau", "base_outer"}
    assert len(stab["neighbor_calmars"]) == 3
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_neighborhood.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.neighborhood'`）。

- [ ] **Step 3: 实现 `discovery/neighborhood.py`**

```python
# -*- coding: utf-8 -*-
"""邻域稳定性（spec §12⑥ / 分层裁判 L3，Plan 1 手动验收版）。

冠军 21 维数值参数 ±perturb 扰动采样，跑 evaluate 看 outer calmar 稳定性。
高原（邻域 calmar 不塌、方差有限）= 稳健放行；孤峰（塌或方差爆炸）= 过拟合否决。
Plan 1 手动验收（verify 命令），不进 calmar_rank 排序（排序留 Plan 3 搜索）。
百分比扰动（×[1-p,1+p]），不依赖 PARAM_SPACE 候选档 → discovery 自洽。
"""
import random

import pandas as pd

from discovery.objective import evaluate


def perturb_params(params, perturb, rng, n_dims=3):
    """随机选 n_dims 个数值参数 ×[1-perturb, 1+perturb]。None 参数保留。
    int 参数（window/min_touches 等）扰动后 round 保 int——否则 scan_symbol 的
    range(window) 收到 float 会 TypeError。"""
    nb = dict(params)
    numeric_keys = [k for k, v in params.items() if isinstance(v, (int, float))]
    keys = rng.sample(numeric_keys, k=min(n_dims, len(numeric_keys)))
    for k in keys:
        new_v = params[k] * rng.uniform(1 - perturb, 1 + perturb)
        nb[k] = round(new_v) if isinstance(params[k], int) else new_v
    return nb


def neighborhood_stability(params, universe, split, perturb=0.15, n_samples=5, seed=42):
    """冠军邻域 ±perturb 扰动 n_samples 次，看 outer calmar 稳定性。

    返回 {base_calmar, neighbor_calmars, neighbor_mean, std, is_plateau, base_outer}。
    is_plateau 判据：邻域 calmar 均值 ≥ base×0.5（不塌过半）。孤峰（均值远低于 base）
    → is_plateau=False → spec §12⑥ 否决。
    """
    rng = random.Random(seed)
    base_outer = evaluate(params, universe, split)["outer"]
    base_c = base_outer["calmar"]
    neighbor_calmars = []
    for _ in range(n_samples):
        nb = perturb_params(params, perturb, rng)
        neighbor_calmars.append(evaluate(nb, universe, split)["outer"]["calmar"])
    s = pd.Series(neighbor_calmars)
    mean = float(s.mean())
    std = float(s.std()) if len(s) > 1 else 0.0
    is_plateau = (mean >= base_c * 0.5) if base_c > 0 else (mean >= 0)
    return {"base_calmar": base_c, "neighbor_calmars": neighbor_calmars,
            "neighbor_mean": mean, "std": std, "is_plateau": bool(is_plateau),
            "base_outer": base_outer}
```

- [ ] **Step 4: 给 cli 加 `verify` 子命令**

在 `discovery/cli.py` 的 `main()` 加 verify 子命令（cmd_verify）:
```python
def cmd_verify(args):
    """邻域稳定性 + 基线对照验收闸（spec §12 ⑤⑥）。"""
    from discovery.neighborhood import neighborhood_stability
    universe, meta = freeze()
    split = holdout_split(args.embargo)
    state = json.load(open(STATE_FILE, encoding="utf-8"))
    params = state["best"]
    print(f"=== discovery verify：邻域稳定性 + 基线对照（snapshot={meta.snapshot_hash}）===")
    stab = neighborhood_stability(params, universe, split,
                                  perturb=args.perturb, n_samples=args.n_samples)
    print(f"冠军 outer: calmar={stab['base_calmar']:.2f} ann={stab['base_outer']['ann']*100:.1f}% "
          f"夏普{stab['base_outer']['sharpe']:.2f} 回撤{stab['base_outer']['max_dd']*100:.1f}%")
    print(f"邻域 {args.n_samples}×±{args.perturb:.0%} 扰动 calmar: "
          f"mean={stab['neighbor_mean']:.2f} std={stab['std']:.2f}")
    verdict = "是（高原稳健，放行）" if stab["is_plateau"] else "否（孤峰，spec §12⑥ 否决——冠军是过拟合尖峰）"
    print(f"邻域稳定性判定: {verdict}")
    print(f"基线对照: state best_ann(全段)={state.get('best_ann', 0)*100:.1f}% "
          f"vs outer 2026 ann={stab['base_outer']['ann']*100:.1f}% "
          f"（去偏幅度见 spec §1.4）")
```

在 `main()` 的子解析器注册加:
```python
    ap_v = sub.add_parser("verify", help="邻域稳定性 + 基线对照验收闸")
    ap_v.add_argument("--embargo", type=int, default=5)
    ap_v.add_argument("--perturb", type=float, default=0.15, help="邻域扰动幅度（默认 15%）")
    ap_v.add_argument("--n-samples", type=int, default=5, dest="n_samples")
    ap_v.set_defaults(func=cmd_verify)
```

- [ ] **Step 5: 跑测试确认通过 + 手动 verify 冒烟**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_neighborhood.py -v
.venv310/Scripts/python.exe -m discovery verify --n-samples 5   # 手动（~15min，5 邻域×3min）
```
Expected: 3 测试 passed；手动 verify 打印邻域稳定性判定（高原/孤峰）+ 基线对照。

- [ ] **Step 6: Commit**

```bash
git add discovery/neighborhood.py discovery/cli.py tests/discovery/test_neighborhood.py
git commit -m "feat(discovery): 邻域稳定性验收闸 + verify 命令（spec §12⑥）

- neighborhood_stability：冠军 ±perturb 扰动，看 outer calmar 稳定性
- 高原（不塌）放行 / 孤峰（塌）否决——防过拟合尖峰
- 百分比扰动 discovery 自洽（不依赖 PARAM_SPACE 候选档）
- verify 命令串邻域 + 基线对照

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review（写完后自查，对照 spec v1.3）

**1. Spec 覆盖**：
- §6.1 L0 快照冻结 → Task 1 ✓
- §3.3 嵌套切分（Plan 1 简化为 2025/2026 holdout + embargo）→ Task 2 ✓（4 折 walk-forward 显式标注推后续 plan）
- §6.2 objective/evaluate_champion 信息隔离 → Task 3 ✓（evaluate inner/outer 派生自同 all_filled，隔离在调用方）
- §3.4 SQLite 三表 → Task 4 ✓（Plan 1 精简字段）
- §3.5 分层裁判 L0闸+L1 calmar → Task 5 ✓（L2 DSR / L3 邻域排序留后续；邻域在 Task 7 手动验收）
- §12 第2条 L1 验收锚（当前冠军去偏）→ Task 6 ✓
- §12⑥ 邻域稳定性 → Task 7 ✓
- §3.3 熊市一票否决 → **Plan 1 标 N/A**（2025-2026 无熊市数据），待后续 plan 扩 regime 数据 ✓（显式标注，非遗漏）
- §1.4 漂移实证 → Task 1 snapshot_hash 直接回应 ✓

**2. Placeholder 扫描**：无 TBD/TODO；每步含完整 test + impl 代码 + 命令。✓

**3. 类型一致性**：`metrics_of` 返回 dict（含 calmar）在 Task 3 定义，Task 5 judging / Task 7 neighborhood 消费——键名 `max_dd`/`n`/`calmar`/`ann` 一致 ✓；`Segment.covers` 在 Task 2 定义、Task 3 segment_metrics 消费 ✓；`evaluate` 返回 `{"inner","outer","n_total"}` 在 Task 3/6/7 一致 ✓。

**Plan 1 显式不做（后续 plan）**：4 折 walk-forward、熊市 regime 覆盖、L2 DSR、L3 邻域进排序、ProcessPool 并发（L2）、Sobol/TPE 搜索（L3）、Pareto+收敛判据+schtasks（L4）、experiment DRAFT 闭环（L5）。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-24-discovery-credibility-l0-l1.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 每个 task 派 fresh subagent，task 间 review，fast iteration（适合 7 个独立 task + TDD）。

**2. Inline Execution** - 本 session 内用 executing-plans 批量执行 + checkpoint review。

Which approach?
```
