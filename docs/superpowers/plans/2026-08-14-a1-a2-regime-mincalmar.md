# A1+A2 实施计划：regime 闸产品化 + 各年 min calmar 搜索目标（DG-G4 落地）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec `2026-08-14-a1-a2-regime-and-mincalmar-design.md` 落地 A2（搜索目标 inner calmar → 各年 min calmar，窗口扩 2021+）与 A1（HS300 MA200 + 宽度 0.5 双确认 regime 闸，eod/pre_open 双前置，UNKNOWN fail-closed）。A2 须在今晚 02:00 daemon 新基线首搜前合入 master。

**Architecture:** A2 在 `discovery/objective.py` 新增 `yearly_metrics`（inner 段按自然年分块复用 `metrics_of`），`evaluate` 的 inner dict 注入 `yearly_calmar`/`min_yearly_calmar`；`discovery/split.py` 新增 `extended_split()`（inner 2021-2024 / outer 2025-2026，不动 `holdout_split` 的 45 个 caller）；score 消费点（`search.py::tpe_search_batch` tell 值 + runner seed_values）切 `min_yearly_calmar`（带 calmar 兼容回退）。A1 新建 `trading/compute/regime.py` 纯函数模块（classify 三态 + 当日缓存），接入 `engine._pipeline_then_eod` 的 `_eod()` 前置与 `_pre_open_gate` ④ 段。

**Tech Stack:** Python 3.10（`.venv310`）、pandas/numpy、pytest。零新增依赖。

## Global Constraints

- 全中文注释，像素级说明 Why（CLAUDE.md）。
- **DG-G4 红线：regime 阈值（MA200/宽度 0.5）模块常量固定，绝不进 TPE/PARAM_SPACE。**
- **信息隔离红线：outer metrics 仍只进报告；yearly 分块仅作用于 inner。**
- **UNKNOWN fail-closed：数据缺失时 regime 停手（等同 BEAR），绝不放行。**
- A1 只断新单（eod 产计划 + pre_open 挂单），不碰存量持仓的止损/止盈。
- 测试：`PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest`（Windows GBK 管道）。
- 分支 `opt/a1-a2-regime`；每 Task 独立 commit。file:line 基准 2026-08-14，实施前以符号名 re-verify。
- inner 某年 n<30 笔 → 该年 calmar 记 0.0（保守，不剔除=不逃考）。

## File Structure

| 文件 | 职责 | 改动 |
|---|---|---|
| `discovery/split.py` | 验证切分 | 新增 `extended_split()`（Task 1） |
| `discovery/objective.py` | 评估函数 | 新增 `yearly_metrics()`；`evaluate` 注入 yearly 字段（Task 1） |
| `discovery/search.py` | TPE 搜索 | `tpe_search_batch` tell 值切 min_yearly_calmar（Task 2） |
| `discovery/runner.py` | 跑批主循环 | seed_values 构造切 min_yearly_calmar（Task 2，执行时以 `seed_values` 定位） |
| `discovery/cli.py` | CLI 装配 | cmd_run/cmd_daemon 切 `extended_split` + lake_start 默认 2021-01-01（Task 2） |
| `discovery/daemon.py` | 夜跑编排 | `estimate_budget` per_group_seconds 40→110（Task 2） |
| `trading/compute/regime.py` | **新建** A1 regime 闸 | classify 三态 + 当日缓存（Task 4） |
| `trading/engine.py` | 引擎 | `_pipeline_then_eod` eod 前置 + `_pre_open_gate` ④ 段（Task 5） |
| `tests/discovery/test_objective.py` | 客观函数测试 | Task 1 增例 |
| `tests/discovery/test_search.py` | 搜索测试 | Task 2 增例 |
| `tests/trading/test_regime.py` | **新建** | Task 4 TDD |
| `tests/trading/test_engine_regime_gate.py` | **新建** | Task 5 TDD |

---

### Task 1：extended_split + yearly_metrics + evaluate 注入（TDD）

**Files:**
- Modify: `discovery/split.py`（`holdout_split` 之后、P5 注释块之前插入）
- Modify: `discovery/objective.py`（`segment_metrics` 之后插 `yearly_metrics`；`evaluate` 改造）
- Test: `tests/discovery/test_objective.py`

**Interfaces:**
- Produces: `extended_split(embargo_days=5) -> HoldoutSplit`（inner 2021-01-01~2024-12-31 / outer 2025-01-01~2026-12-31）
- Produces: `yearly_metrics(all_filled, segment, min_trades=30) -> dict[int, float]`（年份→calmar，n<30 年份记 0.0）
- Produces: `evaluate` 返回的 `inner` dict 新增键 `yearly_calmar: dict[int,float]`、`min_yearly_calmar: float`（无 inner 信号年份时 0.0）

- [ ] **Step 1: 写失败测试**（追加到 `tests/discovery/test_objective.py`）

```python
# ============ A2（DG-G4 · 2026-08-14）：各年 min calmar ============
from datetime import date
from discovery.split import extended_split, Segment
from discovery.objective import yearly_metrics, evaluate


class TestExtendedSplit:
    def test_inner_spans_2021_2024_outer_2025_2026(self):
        """扩展切分：inner 四年（含 2022 熊市考场）/ outer 两年（去偏锚）。"""
        sp = extended_split()
        assert sp.inner.start == date(2021, 1, 1) and sp.inner.end == date(2024, 12, 31)
        assert sp.outer.start == date(2025, 1, 1) and sp.outer.end == date(2026, 12, 31)
        assert sp.embargo_days == 5

    def test_holdout_split_untouched(self):
        """既有二段切分（45 caller 对照锚）不被扩展切分污染。"""
        from discovery.split import holdout_split
        old = holdout_split()
        assert old.inner.start == date(2025, 1, 1) and old.outer.start == date(2026, 1, 1)


class TestYearlyMetrics:
    def _filled(self, year, n, pnl):
        """合成 n 笔 signal_date 全在 year 年、每笔 pnl 的 all_filled 条目。"""
        import pandas as pd
        return [{"avg_pnl_pct": pnl, "signal_date": pd.Timestamp(date(year, 6, 1) if i % 2 else date(year, 3, 1))}
                for i in range(n)]

    def test_min_takes_worst_year(self):
        """两年一好一差 → yearly dict 含两年、min 取差年（好年不被淹没）。"""
        seg = Segment("t", date(2021, 1, 1), date(2024, 12, 31))
        filled = self._filled(2021, 50, 5.0) + self._filled(2022, 50, -2.0)
        ym = yearly_metrics(filled, seg)
        assert 2021 in ym and 2022 in ym
        assert ym[2022] < ym[2021]            # 差年 calmar 更低
        assert min(ym.values()) == ym[2022]   # min 恰取差年

    def test_sparse_year_scores_zero(self):
        """n<30 的年份记 0.0（保守：信号缺席=逃考失败，不剔除）。"""
        seg = Segment("t", date(2021, 1, 1), date(2024, 12, 31))
        filled = self._filled(2021, 50, 5.0) + self._filled(2022, 10, 5.0)  # 2022 仅 10 笔
        ym = yearly_metrics(filled, seg, min_trades=30)
        assert ym[2022] == 0.0

    def test_out_of_segment_excluded(self):
        """segment 外年份不计入（与 segment_metrics 同界语义）。"""
        seg = Segment("t", date(2021, 1, 1), date(2024, 12, 31))
        filled = self._filled(2021, 50, 5.0) + self._filled(2025, 50, 5.0)  # 2025 在 outer
        assert 2025 not in yearly_metrics(filled, seg)

    def test_evaluate_injects_yearly_fields(self):
        """evaluate 的 inner dict 注入 yearly_calmar + min_yearly_calmar（monkeypatch run_full_scan）。"""
        import pandas as pd
        from unittest.mock import patch
        filled = self._filled(2021, 40, 5.0) + self._filled(2022, 40, -1.0) \
            + self._filled(2025, 40, 8.0)  # 2025 属 outer，不进 inner yearly
        with patch("discovery.objective.run_full_scan", return_value=filled):
            res = evaluate({}, {}, extended_split())
        assert set(res["inner"]["yearly_calmar"]) == {2021, 2022, 2023, 2024} - {2023} or \
            2023 not in res["inner"]["yearly_calmar"]   # 无信号年不入 dict
        assert res["inner"]["min_yearly_calmar"] == min(res["inner"]["yearly_calmar"].values())
```

（注：`test_evaluate_injects_yearly_fields` 的 2023 无信号年断言按实现语义「无信号年不入 dict」写；若实现选择「入 dict 记 0」则断言 `ym[2023] == 0.0`——以「无信号年不入 dict」为准，min 不被无信号年拖累为 0 更合理？**否**——裁定：无信号年**不入 dict**（年份由实际信号决定；整 inner 无信号 → min_yearly_calmar=0.0 由 evaluate 兜底）。实现与测试都按此裁定。）

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/discovery/test_objective.py -k "ExtendedSplit or YearlyMetrics" -v`
Expected: FAIL（ImportError: extended_split/yearly_metrics 不存在）

- [ ] **Step 3: 实现 split.py 的 extended_split**（插在 `holdout_split` 之后）

```python
# ============================================================================
# A2（2026-08-14 · DG-G4）：扩展切分——inner 各年 min calmar 的考场
# ============================================================================
def extended_split(embargo_days=5):
    """A2 扩展切分：inner 2021-2024（四个自然年，含 2022 熊市考场）/ outer 2025-2026。

    Why 扩窗：inner 只有 2025 一个自然年时「各年 min」退化为单年，无判别力——
    2026-08-14 实弹验证冠军 25c602 的 wf 四折折外 0.00/-0.62/1.58/3.93，整段
    inner calmar 44.87 全靠选择段（2025）撑起，「2025 特化」坐实。扩到 2021 起
    让 2022 单边熊 + 2023 震荡 + 2024 结构牛都成为考场，min 才有牙齿。

    Why 不改 holdout_split 默认：45 个 caller（compute_unit/publish/proposals/cli）
    依赖 2025/2026 口径作对照锚（含 oos/wf4 交叉验证）；扩展切分仅搜索侧
    （cmd_run/cmd_daemon）启用，_split_tag 自然产 'holdout_2021_2025' 区分新旧。

    Why 2021 起非 2020：湖深 P0-2 实证 2016 起全覆盖；2020 单边牛对 min-calmar
    无增量判别力（牛年人人及格），多一年数据量 +33% 纯耗预算；2021 抱团瓦解
    震荡年恰是好的第一考场。
    """
    return HoldoutSplit(
        inner=Segment("inner_2021_24", date(2021, 1, 1), date(2024, 12, 31)),
        outer=Segment("outer_2025_26", date(2025, 1, 1), date(2026, 12, 31)),
        embargo_days=embargo_days,
    )
```

- [ ] **Step 4: 实现 objective.py 的 yearly_metrics + evaluate 注入**（`segment_metrics` 之后插函数；`evaluate` 返回 dict 前改造）

```python
def yearly_metrics(all_filled, segment, min_trades=30):
    """A2（DG-G4）：segment 内 all_filled 按信号自然年分块 → {年: calmar}。

    Why 按年 min：整段 calmar 被单一大年淹没（2025 特化教训），按年分块让参数
    必须在【每一个】年份站得住才得高分。
    Why n<min_trades 记 0.0（不剔除）：剔除=逃考——信号稀疏年恰是参数在该年不适配
    的证据，记 0 让它拖累 min（保守，不奖励缺席）。
    无信号年份不入 dict（年份由实际信号决定，非日历枚举）。
    """
    from collections import defaultdict
    by_year = defaultdict(list)
    for r in all_filled:
        d = pd.to_datetime(r["signal_date"])
        if segment.covers(d):
            by_year[d.year].append((r["avg_pnl_pct"], d))
    out = {}
    for y in sorted(by_year):
        m = metrics_of(by_year[y])
        out[y] = m["calmar"] if m["n"] >= min_trades else 0.0
    return out
```

`evaluate` 改为：

```python
def evaluate(params, universe, split):
    """评估给定 params 的 inner/outer 两段。

    A2（DG-G4）：inner dict 注入 yearly_calmar（各自然年 calmar，n<30 年记 0）与
    min_yearly_calmar（搜索排序新目标）——整段 calmar 字段保留（feasibility_gate
    等既有消费者兼容）。信息隔离不变：outer 只进报告。
    """
    all_filled = run_full_scan(params, universe)
    inner_m = segment_metrics(all_filled, split.inner, embargo_days=0)
    yearly = yearly_metrics(all_filled, split.inner)
    inner_m["yearly_calmar"] = yearly
    inner_m["min_yearly_calmar"] = min(yearly.values()) if yearly else 0.0
    return {
        "inner": inner_m,
        "outer": segment_metrics(all_filled, split.outer, embargo_days=split.embargo_days),
        "n_total": len(all_filled),
    }
```

- [ ] **Step 5: 跑测试确认通过 + 既有测试零回归**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/discovery/test_objective.py tests/discovery/ -q`
Expected: 新增 6 例 PASS + 既有全绿（inner 加键是纯增量，不破坏既有断言）

- [ ] **Step 6: Commit**

```bash
git add discovery/split.py discovery/objective.py tests/discovery/test_objective.py
git commit -m "feat(a2): extended_split(2021+) + yearly_metrics——inner 各年 calmar 分块与 min 目标数据源（DG-G4）"
```

---

### Task 2：score 消费点切换 + CLI/daemon 装配 + 预算常数

**Files:**
- Modify: `discovery/search.py:165`（tell 值）
- Modify: `discovery/runner.py`（阶段一 seed_values 构造点——执行时 `grep -n "seed_values" discovery/runner.py` 定位，同款切换）
- Modify: `discovery/cli.py`（cmd_run 与 cmd_daemon 两处：`split = holdout_split(...)` → `extended_split(...)`；argparse `--lake-start` default `"2025-01-01"` → `"2021-01-01"`，仅 run/daemon 两个 subparser）
- Modify: `discovery/daemon.py`（`estimate_budget` 的 `per_group_seconds = 40` → `110` + 注释）
- Test: `tests/discovery/test_search.py`、`tests/discovery/test_runner.py`

**Interfaces:**
- Consumes: Task 1 的 `min_yearly_calmar` 键（搜索消费侧）
- Produces: `tpe_search_batch` tell 值 = `min_yearly_calmar`（dict 无该键时回退 `calmar`——合成/旧结果兼容）

- [ ] **Step 1: 写失败测试**（追加到 `tests/discovery/test_search.py`）

```python
class TestTpeBatchMinYearlyCalmar:
    def test_tell_uses_min_yearly_when_present(self):
        """A2：inner 含 min_yearly_calmar 时 TPE tell 它（非整段 calmar）。"""
        from discovery.search import tpe_search_batch
        param_space = [("w", [10, 20, 30])]
        seen = []

        def fake_eval(plist):
            return [{"inner": {"calmar": 99.0, "min_yearly_calmar": 1.5, "outer": {}}} for p in plist]

        pairs, study = tpe_search_batch(
            [{"w": 10}], [0.5], fake_eval, n_trials=2, seed=7, param_space=param_space, batch_size=2)
        vals = [t.value for t in study.trials if t.value is not None]
        # seed 值 0.5 外，新 trial 的 tell 值应全为 1.5（min_yearly），绝不出现 99.0
        assert all(v in (0.5, 1.5) for v in vals)

    def test_tell_falls_back_to_calmar(self):
        """兼容回退：无 min_yearly_calmar 键的旧形态 dict 落回 calmar（既有单测零改）。"""
        from discovery.search import tpe_search_batch
        param_space = [("w", [10, 20, 30])]

        def fake_eval(plist):
            return [{"inner": {"calmar": 3.0}, "outer": {}} for p in plist]

        pairs, study = tpe_search_batch(
            [{"w": 10}], [0.5], fake_eval, n_trials=1, seed=7, param_space=param_space, batch_size=1)
        vals = [t.value for t in study.trials if t.value is not None]
        assert 3.0 in vals
```

（runner 侧同款：`tests/discovery/test_runner.py` 中 seed_values 构造断言——若既有 `_mock_search_deps` 已 fake 掉 tpe 层，则 runner 侧改为断言「阶段一 top 排序取 min_yearly_calmar」；执行时按 runner 现有测试 seam 就近补 1 例，断言排序键切换。） 

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/discovery/test_search.py -k MinYearly -v`
Expected: FAIL（tell 的是 calmar=99.0，断言 `all(v in (0.5, 1.5))` 不成立）

- [ ] **Step 3: 实现**——`search.py:165` 改：

```python
                # A2（DG-G4）：排序目标切各年 min calmar（2025 特化教训——整段 calmar
                # 被 single 大年淹没）。兼容回退：无该键的旧/合成 dict 落回 calmar。
                _inner = res["inner"]
                study.tell(tr, float(_inner.get("min_yearly_calmar",
                                                _inner.get("calmar", 0.0))))
```

runner.py 的 seed_values 构造点同款切换（`grep -n "seed_values" discovery/runner.py` 定位后，把从 `r["inner"]["calmar"]` 取值处改为 `r["inner"].get("min_yearly_calmar", r["inner"].get("calmar", 0.0))`，注释同款）。

cli.py（cmd_run 与 cmd_daemon 各一处）：

```python
    # A2（2026-08-14）：搜索侧切扩展切分（inner 2021-2024 含熊市考场）+ 湖窗 2021 起；
    # oos/wf 等 45 个 holdout_split caller 的对照口径（2025/2026）不动。
    split = extended_split(args.embargo)
```

（import 行补 `from discovery.split import extended_split`；`--lake-start` default 改 `"2021-01-01"` 仅限 run/daemon 两个 subparser。）

daemon.py `estimate_budget`：`per_group_seconds = 40` → `110`，注释追加「A2 窗口 2025+→2021+ 数据量 ×3，P1 后 35.5s/组 ×3 预估 110s；diag 冒烟实测后校正”。

- [ ] **Step 4: 跑测试确认通过 + discovery 全量**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/discovery/ -q`
Expected: 全绿（含既有 12 例 runner + 9 例 search）

- [ ] **Step 5: Commit**

```bash
git add discovery/search.py discovery/runner.py discovery/cli.py discovery/daemon.py tests/discovery/test_search.py tests/discovery/test_runner.py
git commit -m "feat(a2): 搜索目标切 min_yearly_calmar（tell/seed_values 兼容回退）+ run/daemon 切 extended_split 与 2021 湖窗"
```

---

### Task 3：A2 实证验收——25c602 新口径重跑 + 单组测速

**Files:**
- Create: `diag/a2_mincalmar_probe.py`（一次性实证脚本，同 diag/ 既有范式）

- [ ] **Step 1: 写实证脚本**

```python
# -*- coding: utf-8 -*-
"""A2 实证：25c602 冠军参数在扩展口径（inner 2021-2024）下的各年 calmar 分解。

物理意图：验证「新目标确有判别力」——旧口径 inner(2025) calmar 44.87 的冠军，
在四年考场下 min 应显著塌陷（2022 熊市年预期 ≈0 或负）。同时录单组耗时
（P1 后 35.5s/组是 2025+ 窗口口径，扩窗 ×3 的实测数供 estimate_budget 校正）。
"""
import sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
from discovery.snapshot import freeze
from discovery.split import extended_split
from discovery.objective import evaluate
import json, sqlite3

conn = sqlite3.connect("experiment/experiments.db")
conn.row_factory = sqlite3.Row
params = json.loads(conn.execute(
    "SELECT params FROM experiment_version WHERE status='ACTIVE'").fetchone()["params"])

t0 = time.time()
universe, meta = freeze("2021-01-01")
t_freeze = time.time() - t0
t1 = time.time()
res = evaluate(params, universe, extended_split())
t_eval = time.time() - t1
print(f"universe={meta.universe_count} freeze={t_freeze:.1f}s eval={t_eval:.1f}s")
print("inner 整段:", {k: round(v, 3) if isinstance(v, float) else v for k, v in res["inner"].items() if k != "yearly_calmar"})
print("各年 calmar:", res["inner"]["yearly_calmar"])
print("min_yearly_calmar:", round(res["inner"]["min_yearly_calmar"], 3))
```

- [ ] **Step 2: 跑实证**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe diag/a2_mincalmar_probe.py`
Expected: `min_yearly_calmar` 显著低于 44.87（熊市/震荡年塌陷）；`eval` 耗时记录回填 `estimate_budget`（若 >180s 需回 spec 议收窗 2022+）。

- [ ] **Step 3: 结果回填 spec 验收节 + Commit**

```bash
git add diag/a2_mincalmar_probe.py docs/superpowers/specs/2026-08-14-a1-a2-regime-and-mincalmar-design.md
git commit -m "diag(a2): 25c602 扩展口径各年 calmar 实证——新目标判别力验收 + 单组耗时锚"
```

---

### Task 4：trading/compute/regime.py 纯函数模块（TDD）

**Files:**
- Create: `trading/compute/regime.py`
- Create: `tests/trading/test_regime.py`

**Interfaces:**
- Produces: `classify(index_df=None, daily_df=None, asof=None) -> RegimeState`；`RegimeState(state: str, reason: str, asof: str)`，state ∈ {"BULL", "BEAR", "UNKNOWN"}
- Produces: 常量 `MA_WINDOW=200`、`BREADTH_THRESHOLD=0.5`、`BREADTH_MIN_STOCKS=500`、`HS300="000300.SH"`（DG-G4 定稿，绝不进 TPE）
- Consumes: `data_lake/index_daily.parquet`（index=[date,symbol]，xs 000300.SH）与 `data_lake/a_shares_daily.parquet`（df 参数可注入供测试；None 时读湖）

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""A1 regime 闸单测（DG-G4：HS300>MA200 ∧ 宽度>0.5 双确认，UNKNOWN fail-closed）。"""
import pandas as pd
import pytest
from datetime import date
from trading.compute.regime import classify, RegimeState, MA_WINDOW, BREADTH_THRESHOLD


def _index_df(closes):
    """合成单标的指数 df（index=[date,symbol] 对齐 index_daily 湖 schema）。"""
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2020-01-01", periods=len(closes), freq="B"), ["000300.SH"]],
        names=["date", "symbol"])
    return pd.DataFrame({"close": closes}, index=idx)


def _daily_df(n_symbols, above_frac, n_days=220):
    """合成宽度数据：n_symbols 只标的各 n_days 根，above_frac 比例的标的全程>自身MA200。"""
    rows, symbols = [], []
    n_above = int(n_symbols * above_frac)
    for i in range(n_symbols):
        sym = f"{600000+i}.SH"
        symbols.append(sym)
        # above 标的恒 100→缓慢上移（确保 close>MA200）；below 标的恒 100→缓慢下移
        drift = 0.05 if i < n_above else -0.05
        closes = [100 + drift * k for k in range(n_days)]
        for k, c in enumerate(closes):
            rows.append((pd.Timestamp("2020-01-01") + pd.Timedelta(days=k), sym, c))
    df = pd.DataFrame(rows, columns=["date", "symbol", "close"])
    return df.set_index(["date", "symbol"])


class TestClassify:
    def test_bull_when_price_above_ma200_and_breadth_confirms(self):
        idx = _index_df(list(range(100, 100 + 260)))          # 严格上行 → close>MA200
        daily = _daily_df(600, 0.8)                            # 宽度 80% > 0.5
        st = classify(index_df=idx, daily_df=daily)
        assert st.state == "BULL"

    def test_bear_when_price_below_ma200(self):
        idx = _index_df(list(range(360, 100, -1)))            # 严格下行 → close<MA200
        daily = _daily_df(600, 0.8)
        st = classify(index_df=idx, daily_df=daily)
        assert st.state == "BEAR"

    def test_bear_when_breadth_fails(self):
        idx = _index_df(list(range(100, 360)))                # 指数多头
        daily = _daily_df(600, 0.3)                            # 宽度 30% < 0.5 → 双确认失败
        st = classify(index_df=idx, daily_df=daily)
        assert st.state == "BEAR"

    def test_unknown_when_index_history_too_short(self):
        """指数 <MA200 根 → UNKNOWN（fail-closed 由调用方执行：停手）。"""
        idx = _index_df(list(range(100, 300)))                # 仅 200 根（<200+1 无 MA）
        daily = _daily_df(600, 0.8)
        st = classify(index_df=idx, daily_df=daily)
        assert st.state == "UNKNOWN"

    def test_unknown_when_breadth_sample_too_small(self):
        """宽度统计标的 < BREADTH_MIN_STOCKS（数据残缺）→ UNKNOWN。"""
        idx = _index_df(list(range(100, 360)))
        daily = _daily_df(100, 0.8)                            # 仅 100 只
        st = classify(index_df=idx, daily_df=daily)
        assert st.state == "UNKNOWN"

    def test_reason_is_human_readable(self):
        st = classify(index_df=_index_df(list(range(360, 100, -1))), daily_df=_daily_df(600, 0.8))
        assert st.reason and isinstance(st.reason, str) and ("MA200" in st.reason or "200" in st.reason)

    def test_same_day_cached(self):
        """同日多次调用共享结果（读 455MB 湖一次的成本不能每个触发点都付）。"""
        idx = _index_df(list(range(100, 360)))
        daily = _daily_df(600, 0.8)
        s1 = classify(index_df=idx, daily_df=daily)
        s2 = classify()   # 无注入 → 命中当日缓存（不重读湖）
        assert s1.asof == s2.asof and s1.state == s2.state
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_regime.py -v`
Expected: FAIL（ModuleNotFoundError: trading.compute.regime）

- [ ] **Step 3: 实现 regime.py**

```python
# -*- coding: utf-8 -*-
"""A1 市场状态闸（DG-G4 定稿 · 2026-08-14）：沪深300 200 日均线 + 市场宽度双确认。

物理意图：颈线法是突破策略，wf 四折实证其冠军参数在 2022 熊市折外 calmar=-0.62
（熊市负期望坐实）——空头环境假突破多，正确动作是停手不进场。本模块给执行侧
（engine._eod 选股前置 + _pre_open_gate ④ 段）提供单源的 regime 判定。

判据（DG-G4 红线：阈值固定经验值，绝不进 TPE/PARAM_SPACE——否则搜索会过拟合
regime 参数本身）：
    BULL   = 沪深300 收盘 > MA200  ∧  宽度 > 0.5        → 允许新单
    BEAR   = 任一不满足                                → 停手（只断新单，存量退出照常）
    UNKNOWN = 数据缺失（指数 <200 根 / 宽度样本 <500 只 / 读湖异常）
             → 调用方 fail-closed 视同 BEAR（DG-G3 哲学：缺信息时收紧）

宽度 = a_shares_daily 全市场 close > 各自 MA200 的标的占比（A股整体多空结构，
防「指数靠权重股撑、市场实际已转空」的假多头）。

当日缓存：宽度计算要读 455MB 主湖，一天只算一次（eod 一次 + pre_open 复用）；
盘中新数据不改当日判定（regime 是环境闸非交易信号，日内翻转由次日判定吸收）。
"""
from __future__ import annotations

from dataclasses import dataclass

# DG-G4 定稿常量（红线：绝不进 TPE）
MA_WINDOW = 200            # 200 日均线（牛熊分界主流口径）
BREADTH_THRESHOLD = 0.5    # 宽度过半 = 市场多数标的在年线上方
BREADTH_MIN_STOCKS = 500   # 宽度统计最小样本（防残缺湖误判「假宽度」）
HS300 = "000300.SH"

_BREADTH_TAIL_DAYS = 260   # 宽度计算只取近 260 交易日（MA200 需 200 根 + 余量，
                           # 全历史 groupby 纯浪费）


@dataclass(frozen=True)
class RegimeState:
    """regime 判定结果（不可变值对象）。state ∈ {BULL, BEAR, UNKNOWN}。"""
    state: str
    reason: str   # 人读中文（含数据细节，供播报/日志定位）
    asof: str     # 判定所据最后交易日 ISO（观测面）


# 当日缓存（进程内）：{asof: RegimeState}——同日多触发点共享一次读湖。
_CACHE: dict[str, RegimeState] = {}


def classify(index_df=None, daily_df=None, asof=None) -> RegimeState:
    """三态 regime 判定。df 参数注入供测试；None 时读 data_lake 两湖。

    asof：显式指定判定基准日（测试/回放用）；None = 湖数据最新日（生产路径）。
    读湖/计算异常 → UNKNOWN（fail-closed 语义由调用方执行停手）。
    """
    key = asof or "latest"
    if key in _CACHE:                       # 当日缓存命中（回放模式 asof 各异不互撞）
        return _CACHE[key]
    try:
        if index_df is None or daily_df is None:
            index_df, daily_df = _load_lake()
        st = _classify_sync(index_df, daily_df, asof)
    except Exception as exc:                # 读湖/对齐任何异常 → UNKNOWN 不放行
        st = RegimeState("UNKNOWN", f"regime 判定异常（fail-closed）：{exc!r}", str(asof or "?"))
    _CACHE[key] = st
    return st


def _load_lake():
    import pandas as pd
    idx = pd.read_parquet("data_lake/index_daily.parquet")
    daily = pd.read_parquet("data_lake/a_shares_daily.parquet")
    return idx, daily


def _classify_sync(index_df, daily_df, asof) -> RegimeState:
    import pandas as pd
    # ① 指数腿：000300.SH 收盘 vs MA200（min_periods=MA_WINDOW——不足 200 根 NaN）
    try:
        hs = index_df.xs(HS300, level="symbol").sort_index()
    except KeyError:
        return RegimeState("UNKNOWN", f"指数湖无 {HS300}", str(asof or "?"))
    if asof is not None:
        hs = hs[hs.index <= pd.Timestamp(asof)]
    if len(hs) < MA_WINDOW + 1:
        return RegimeState("UNKNOWN",
                           f"指数历史不足 {MA_WINDOW+1} 根（现 {len(hs)}）", str(hs.index[-1].date()))
    ma = hs["close"].rolling(MA_WINDOW, min_periods=MA_WINDOW).mean()
    last_date, last_close, last_ma = hs.index[-1], float(hs["close"].iloc[-1]), float(ma.iloc[-1])
    if pd.isna(last_ma):
        return RegimeState("UNKNOWN", "MA200 为 NaN", str(last_date.date()))
    price_ok = last_close > last_ma

    # ② 宽度腿：全市场 close>各自 MA200 占比（只取近 260 日控计算量）
    tail = daily_df if len(daily_df.index.get_level_values("date").unique()) <= _BREADTH_TAIL_DAYS \
        else daily_df.loc[daily_df.index.get_level_values("date").unique()[-_BREADTH_TAIL_DAYS]:]
    if asof is not None:
        tail = tail[tail.index.get_level_values("date") <= pd.Timestamp(asof)]
    ma_s = tail["close"].groupby(level="symbol").transform(
        lambda s: s.rolling(MA_WINDOW, min_periods=MA_WINDOW).mean())
    valid = tail["close"].notna() & ma_s.notna()
    per_sym = (tail["close"] > ma_s).groupby(level="symbol").any() & \
        valid.groupby(level="symbol").any()      # 每标的末位有效性聚合简化：任一日有效即计
    n_valid = int(per_sym.sum())
    if n_valid < BREADTH_MIN_STOCKS:
        return RegimeState("BEAR" if not price_ok else "UNKNOWN",
                           f"宽度样本不足 {BREADTH_MIN_STOCKS} 只（现 {n_valid}）",
                           str(last_date.date()))
    breadth = float(per_sym.mean())
    breadth_ok = breadth > BREADTH_THRESHOLD

    if price_ok and breadth_ok:
        return RegimeState("BULL",
                           f"HS300 {last_close:.0f}>MA200 {last_ma:.0f}，宽度 {breadth:.0%}",
                           str(last_date.date()))
    why = []
    if not price_ok:
        why.append(f"HS300 {last_close:.0f}≤MA200 {last_ma:.0f}")
    if not breadth_ok:
        why.append(f"宽度 {breadth:.0%}≤{BREADTH_THRESHOLD:.0%}")
    return RegimeState("BEAR", "；".join(why), str(last_date.date()))
```

（注意：宽度腿的 per-sym 聚合用「标的在窗口内任一日 close>MA200」会高估宽度——**修正裁定**：取每标的**最后一根有效 K 线**的 close>MA200 判定（时点宽度，非区间宽度）。实现时把 `per_sym` 改为按 symbol 取 `tail` 末位行的 close vs ma_s 比较：

```python
    last_rows = tail.assign(_ma=ma_s).groupby(level="symbol").tail(1)   # 每标的末位行
    last_rows = last_rows[last_rows["close"].notna() & last_rows["_ma"].notna()]
    n_valid = len(last_rows)
    ...
    breadth = float((last_rows["close"] > last_rows["_ma"]).mean())
```

按此「末位行时点宽度」实现，测试的 `_daily_df` 合成数据（above 标的恒上移/ below 恒下移）与时点语义自洽。）

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_regime.py -v`
Expected: 7 例全 PASS

- [ ] **Step 5: 真湖实弹验证**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -c "from trading.compute.regime import classify; s=classify(); print(s)"`
Expected: 打印当前真实 regime（2026-08 市场——预期 BULL 或 BEAR，reason 含具体数字；UNKNOWN 则修数据）

- [ ] **Step 6: Commit**

```bash
git add trading/compute/regime.py tests/trading/test_regime.py
git commit -m "feat(a1): trading/compute/regime.py——HS300 MA200+宽度双确认三态闸（DG-G4 定稿，UNKNOWN fail-closed）"
```

---

### Task 5：engine 双前置接入（eod 产计划前置 + pre_open_gate ④ 段）

**Files:**
- Modify: `trading/engine.py`（`_pipeline_then_eod` 内 `await self._eod()` 调用前——执行时 `grep -n "_eod()" trading/engine.py trading/*.py` 定位事件链真身；`_pre_open_gate` 的 ③ 段（`get_ready` 检查）之后加 ④ 段）
- Create: `tests/trading/test_engine_regime_gate.py`

**Interfaces:**
- Consumes: Task 4 的 `classify() -> RegimeState`
- Produces: BEAR/UNKNOWN 时 ① `_pipeline_then_eod` skip `engine._eod()`（log + 钉钉软降级播报）② `_pre_open_gate` 返 `(False, f"regime 停手（{state}：{reason}）")`

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""A1 engine 双前置：regime BEAR/UNKNOWN → eod skip + pre_open 拒（TDD）。"""
import pytest
from unittest.mock import AsyncMock, patch
from trading.engine import TradingEngine


@pytest.fixture
def eng():
    return TradingEngine()


class TestEodRegimeGate:
    async def test_bear_skips_eod(self, eng):
        """BEAR → 不调 _eod（skip + 播报），事件链不炸。"""
        with patch("trading.compute.regime.classify",
                   return_value=RegimeStateStub("BEAR", "HS300≤MA200", "2026-08-14")):
            # _pipeline_then_eod 的前置段（pipeline 子进程等待等）已在既测试有 seam；
            # 本测试直接调引擎的 regime 前置包装（实现若抽成 _regime_gate 方法则直接测它）
            ok, reason = await eng._regime_gate()
        assert ok is False and "regime" in reason

    async def test_unknown_also_blocks(self, eng):
        """UNKNOWN fail-closed：数据缺失等同停手。"""
        with patch("trading.compute.regime.classify",
                   return_value=RegimeStateStub("UNKNOWN", "指数历史不足", "2026-08-14")):
            ok, reason = await eng._regime_gate()
        assert ok is False

    async def test_bull_passes(self, eng):
        with patch("trading.compute.regime.classify",
                   return_value=RegimeStateStub("BULL", "双确认通过", "2026-08-14")):
            ok, reason = await eng._regime_gate()
        assert ok is True and reason == ""


class RegimeStateStub:
    def __init__(self, state, reason, asof):
        self.state, self.reason, self.asof = state, reason, asof


class TestPreOpenGateRegime:
    async def test_gate_returns_false_on_bear(self, eng, monkeypatch):
        """④ 段：①②③ 全绿但 regime BEAR → 拒挂单。"""
        async def fake_load_plan(d):
            return {"confirmed": True, "entries": []}
        monkeypatch.setattr("trading.engine.load_plan", fake_load_plan)
        monkeypatch.setattr(eng, "_gw_health_gate", lambda gw: (True, ""))
        with patch("trading.data_ready.get_ready", return_value=True), \
             patch("trading.compute.regime.classify",
                   return_value=RegimeStateStub("BEAR", "宽度 30%", "2026-08-14")):
            ok, reason = await eng._pre_open_gate("2026-08-17", object())
        assert ok is False and "regime" in reason
```

（注意：`load_plan`/`get_ready` 的 patch 路径以 `trading/engine.py` 现有 import 形态为准——执行时先 `grep -n "load_plan\|get_ready" trading/engine.py` 对齐 seam；`_regime_gate` 为本 Task 新增方法，测试先行定义契约。）

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_engine_regime_gate.py -v`
Expected: FAIL（`_regime_gate` 不存在 / gate 无 ④ 段）

- [ ] **Step 3: 实现**——engine.py 新增方法（`_pre_open_gate` 附近）：

```python
    async def _regime_gate(self) -> tuple[bool, str]:
        """A1（DG-G4 · 2026-08-14）：市场状态闸——空头/未知环境停新单。

        物理意图：颈线法冠军参数 2022 熊市折外 calmar=-0.62（wf 四折实证），空头
        环境假突破多，正确动作是停手。BEAR/UNKNOWN（fail-closed，DG-G3 哲学）→
        拒新单；BULL 放行。**只断新单**：存量持仓的止损/止盈/stop_loss 巡检不经
        本闸（退出永远允许——停手≠清仓，闸门不越权变相自动清仓）。
        判定单源 trading.compute.regime.classify（当日缓存，读湖成本一日一次）。
        """
        from trading.compute import regime
        try:
            st = regime.classify()
        except Exception:
            # classify 自身已 fail-closed 返 UNKNOWN；此处兜底极端（import 级）异常
            logger.exception("regime 判定异常（fail-closed 停手）")
            return False, "regime 判定异常（fail-closed 停手）"
        if st.state != "BULL":
            reason = f"regime 停手（{st.state}：{st.reason}）"
            logger.warning("A1 %s", reason)
            return False, reason
        return True, ""
```

`_pipeline_then_eod` 内 `await self._eod()` 之前插入：

```python
        # A1 前置：空头/未知环境不产新计划（空仓过节；存量退出不受影响）。
        rg_ok, rg_reason = await self._regime_gate()
        if not rg_ok:
            # 播报软降级（通知通道故障不能阻断「停手」这个主行为）
            try:
                from infra.notifier import NotificationManager, fire_and_forget
                fire_and_forget(NotificationManager.get_default().notify_risk_event(
                    f"eod 跳过：{rg_reason}", "WARN"))
            except Exception:
                logger.debug("regime 停手播报软降级", exc_info=True)
            return
```

`_pre_open_gate` 在 ③ 数据就绪段（`get_ready` 检查）之后追加 ④：

```python
        # ④ 市场状态（A1 · DG-G4）：eod 后隔夜可能转空，挂单前二次复核（同 classify 单源）。
        rg_ok, rg_reason = await self._regime_gate()
        if not rg_ok:
            return False, rg_reason
        return True, ""
```

（原 `return True, ""` 收尾替换为上述；`grep -n "return True, \"\"" trading/engine.py` 对齐。）

- [ ] **Step 4: 跑测试 + engine 既有测试零回归**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/trading/test_engine_regime_gate.py tests/trading/ -q`
Expected: 新增 PASS + 既有全绿（`test_engine_pre_open_gate` 等既有 gate 测试需 monkeypatch classify 为 BULL 或走 UNKNOWN 拒——**执行时检查既有 gate 测试是否因 ④ 段新增而需要补 classify patch**，原则：既有测试语义「三段全绿放行」更新为「四段全绿放行」，patch classify 返 BULL）

- [ ] **Step 5: Commit**

```bash
git add trading/engine.py tests/trading/test_engine_regime_gate.py tests/trading/
git commit -m "feat(a1): eod 产计划前置 + pre_open_gate ④ 段 regime 复核——空头/未知停新单（fail-closed）"
```

---

### Task 6：2022 熊市段回放验证 + 合入收尾

**Files:**
- Create: `diag/a1_regime_replay_2022.py`

- [ ] **Step 1: 回放脚本**（逐月 classify 2022 全年 + 2024 结构牛对照）

```python
# -*- coding: utf-8 -*-
"""A1 回放验收：regime 闸在 2022 单边熊（应 BEAR）与 2024 结构牛（应 BULL）的历史表现。

物理意图：判据有效性实证——若 2022 年 1-4 月（HS300 单边下杀 -20%+）闸不能全 BEAR，
或 2024 下半年（9-24 行情结构牛）不能转 BULL，则阈值（MA200/0.5）需回 ADR 重议。
指数湖 2021-01 起：2022-01 时 MA200 恰有 ~244 根（边缘可行），2021 内年份不回放。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
import pandas as pd
from trading.compute.regime import classify, _load_lake

idx, daily = _load_lake()
rows = []
for mstart in pd.date_range("2022-01-01", "2024-12-01", freq="MS"):
    asof = (mstart + pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d")
    st = classify(index_df=idx, daily_df=daily, asof=asof)
    rows.append((asof, st.state, st.reason[:40]))
for asof, state, reason in rows:
    print(f"{asof}  {state:<7} {reason}")
bear_2022_h1 = sum(1 for a, s, _ in rows if a < "2022-07" and s == "BEAR")
bull_2024_h2 = sum(1 for a, s, _ in rows if a >= "2024-07" and s == "BULL")
print(f"\n2022 上半年 BEAR 占 {bear_2022_h1}/6（验收 ≥5/6）")
print(f"2024 下半年 BULL 占 {bull_2024_h2}/6（验收 ≥4/6——9-24 前的阴跌期 BEAR 属正确判定）")
```

- [ ] **Step 2: 跑回放**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe diag/a1_regime_replay_2022.py`
Expected: 2022 上半年 BEAR ≥5/6；2024 下半年 BULL ≥4/6。未达标 → 阈值回 spec §2.2 重议（记 ADR）。

- [ ] **Step 3: 合入 master（等价守卫 + 全量测试）**

Run: `PYTHONUTF8=1 .venv310/Scripts/python.exe -m pytest tests/ -q`（全量，对照 1833 基线 + 2 已知存量失败）
Expected: 除既有 2 存量失败外全绿 → `git checkout master && git merge opt/a1-a2-regime`（或按当时分支策略 ff/merge）。

- [ ] **Step 4: 交付注记 Commit + daemon 夜跑确认**

```bash
git add diag/a1_regime_replay_2022.py
git commit -m "diag(a1): 2022 熊市/2024 结构牛 regime 回放验收 + P1-1 新基线首搜注记（min_yearly_calmar 目标生效）"
```

确认 02:00 daemon 首夜跑：integrity 闸过（repair 已收敛）→ 新 engine_hash + min_yearly_calmar 目标 → trial 落库 `split_tag='holdout_2021_2025'`。

---

## Self-Review 记录

1. **Spec 覆盖**：A2 窗口/字段/切换点/预算（Task 1-3）✓；A1 模块/双前置/fail-closed/只断新单/回放验收（Task 4-6）✓；spec §1.3 验收门三件（单测/25c602 对比/daemon smoke）→ Task 1/3/6 ✓。
2. **占位符**：无 TBD/TODO；「执行时 grep 定位」三处（runner seed_values、engine _eod 调用点、gate 测试 seam）均给了 grep 命令 + 目标语义 + 代码，非悬空引用。
3. **类型一致**：`RegimeState(state, reason, asof)` 贯穿 Task 4/5；`min_yearly_calmar` 键名 Task 1/2 一致；`extended_split` 命名一致。
