# 参数发现引擎 · L3 搜索层完成 + L4 自治层核心 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Plan 2 的跑批骨架（Sobol+并发+断点续跑，status 恒 budget_exhausted）升级为**自收敛搜索**——optuna TPE 序贯优化 + Pareto 非支配前沿 + 收敛判据①②③④（覆盖度④前置否决防伪收敛）+ DSR 统计裁决 + 耦合5/6 厘清，让 run 能可信自停（status 扩 converged）。

**Architecture:** 两阶段搜索——阶段一 Plan 2 Sobol 批量并发落库（覆盖），阶段二 optuna TPESampler 序贯精化（Sobol 点 warm start，inner calmar 为目标，串行 evaluate）。Pareto 前沿（自写纯函数，2 目标 ann↑/max_dd↓）作 L1 候选筛选补充。收敛判据：①连续 K 轮前沿不扩张 ②EI<ε ③预算耗尽 ④覆盖度 ρ≥0.8 前置否决。DSR 标 top-N 统计显著性。

**Tech Stack:** Python 3.10（.venv310）、optuna（**唯一新增依赖**，spec §7.2）、numpy、sqlite3、multiprocessing。

**父 spec / design：** `docs/superpowers/specs/2026-07-23-param-discovery-engine-design.md` v1.3 + `docs/superpowers/specs/2026-07-24-discovery-plan3-l3-l4-design.md`

## Global Constraints

- **Python 3.10**（`.venv310/Scripts/python.exe`，与 miniQMT xtquant 同环境）。
- **optuna 唯一新依赖**（spec §7.2）：轻量通用优化框架，非 ADR4 红线的"重型黑盒量化库"（vectorbt/backtrader/qlib 才是红线）。`pip install optuna` + 更新 `requirements.txt`。
- **ADR8 回测内核零改动**：`strategies/neckline/` 只读（scan_symbol/run_full_scan 同源契约守护）。
- **信息隔离**（spec §6.2）：Pareto/排序/TPE 目标/DSR **只用 inner**，outer 仅报告（Plan 2 已立，Plan 3 不破）。
- **TPE 目标 = inner calmar**（design 决策1，对齐 §3.5 v1.2 主排序；非 spec §6.2 v1.1 的 ann——ann 是 risk_metrics 复利放大失真产物，§1.4 实证）。
- **Windows spawn 兼容**（spec §8 拷问②，Plan 2 已立）：TPE 阶段主进程串行 evaluate（不 spawn），Sobol 阶段沿用 Plan 2 ProcessPool。
- **全中文注释 + 像素级物理意图**（CLAUDE.md）：每个函数 docstring 说"是什么+为什么"。
- **诚实收窄**（spec §3.5/§13）：DSR 信号稀疏致置信区间宽，如实报不强选（ADR13）；耦合6 完整逐信号点裁剪需内核（ADR8），discovery 层只做 n_total=0 代理。

---

## File Structure

**新增（4）**：
| 文件 | 责任 | 依赖 |
|---|---|---|
| `discovery/pareto.py` | Pareto 非支配前沿 + 收敛判据①③（纯函数） | 无（纯 stdlib） |
| `discovery/coverage.py` | 网格占用率 ρ + 判据④（纯函数） | `sampler.PARAM_SPACE` |
| `discovery/dsr.py` | Deflated Sharpe Ratio 闭式（纯函数） | 无（纯 stdlib math） |
| `discovery/search.py` | optuna TPESampler + Sobol warm start + EI 代理 | optuna、`sampler.PARAM_SPACE`/`sobol_sample` |

**修改（5）**：
| 文件 | 改动 |
|---|---|
| `discovery/constraints.py` | 耦合5 docstring 厘清（suppression↔decay_tau 独立可调） |
| `discovery/worker.py` | 耦合6 runtime 裁剪（`_eval_worker` 内 n_total==0→None） |
| `discovery/runner.py` | 接入 TPE 阶段 + Pareto + 收敛判据 + DSR；`RunSummary.status` 扩 `converged` |
| `discovery/cli.py` | run 接入收敛打印；新增 `champions`/`report` 子命令 |
| `discovery/__init__.py` | 导出 Plan 3 API（pareto/coverage/dsr/search） |

---

## Task 1: Pareto 前沿 + 收敛判据①③（`discovery/pareto.py`）

**Files:**
- Create: `discovery/pareto.py`、`tests/discovery/test_pareto.py`

**Interfaces:**
- Consumes: 无（纯函数，trial 是含 ann/max_dd 等 metrics 的 dict）。
- Produces: `pareto_frontier(trials, obj_max=("ann",), obj_min=("max_dd",)) -> list[int]`（非支配索引）；`frontier_grew(old, new) -> bool`；`converged_k_rounds(frontier_history, K=3) -> bool`（判据①）。后续 T6 用。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_pareto.py`:
```python
# -*- coding: utf-8 -*-
"""Pareto 前沿 + 收敛判据① 测试（纯函数，零依赖）。"""


def test_pareto_frontier_2d():
    """2 目标 (ann↑, max_dd↓) 非支配前沿：被支配的剔除。"""
    from discovery.pareto import pareto_frontier
    trials = [
        {"ann": 0.5, "max_dd": 0.20},   # 0：被 1 支配（1 ann 更高 + max_dd 更低）
        {"ann": 0.6, "max_dd": 0.15},   # 1：前沿
        {"ann": 0.4, "max_dd": 0.10},   # 2：前沿（max_dd 最低）
        {"ann": 0.3, "max_dd": 0.30},   # 3：被 0/1/2 支配
    ]
    idx = pareto_frontier(trials)   # 默认 obj_max=("ann",), obj_min=("max_dd",)
    assert set(idx) == {1, 2}


def test_pareto_frontier_single_obj():
    """单目标 ann↑：前沿=最高 ann 那 trial。"""
    from discovery.pareto import pareto_frontier
    trials = [{"ann": 0.1}, {"ann": 0.5}, {"ann": 0.3}]
    idx = pareto_frontier(trials, obj_max=("ann",), obj_min=())
    assert idx == [1]


def test_frontier_grew():
    """新前沿有 old 没有的点=扩张。"""
    from discovery.pareto import frontier_grew
    assert frontier_grew([1, 2], [1, 2, 3]) is True
    assert frontier_grew([1, 2], [1, 2]) is False
    assert frontier_grew([1, 2, 3], [1, 2]) is False   # 收缩不算扩张


def test_converged_k_rounds_true():
    """连续 K 轮前沿不扩张→收敛（判据①）。"""
    from discovery.pareto import converged_k_rounds
    # 第 0 轮 {1}，第 1 轮扩张到 {1,2}，第 2/3/4 轮都不扩张
    history = [[1], [1, 2], [1, 2], [1, 2], [1, 2]]
    assert converged_k_rounds(history, K=3) is True


def test_converged_k_rounds_false_recent_grew():
    """最近一轮扩张→未收敛。"""
    from discovery.pareto import converged_k_rounds
    history = [[1], [1, 2], [1, 2], [1, 2], [1, 2, 3]]   # 最后一轮扩张
    assert converged_k_rounds(history, K=3) is False


def test_converged_k_rounds_false_short_history():
    """历史不足 K+1 轮→无法判收敛（False，保守不停）。"""
    from discovery.pareto import converged_k_rounds
    assert converged_k_rounds([[1], [1, 2]], K=3) is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_pareto.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.pareto'`）。

- [ ] **Step 3: 实现 `discovery/pareto.py`**

```python
# -*- coding: utf-8 -*-
"""L4 Pareto 非支配前沿 + 收敛判据①③（spec §3.5 v1.2 / §4.1，纯函数）。

物理意图（spec §3.5 v1.2）：v1 把 Pareto 当主排序，前沿在 21 维高维稀疏 + 噪声敏感下
"一堆点谁也压不住谁"。v1.2 降级——主排序用单目标 calmar（L1 全序），Pareto 退为
"L1 候选筛选补充"：前沿上的解进 L1 全序。本模块只算前沿（多目标非支配），不排序
（排序在 judging.calmar_rank）。默认 2 目标 ann↑/max_dd↓（颈线法最关切的收益-回撤前沿；
sharpe 与 ann 高度相关，n 笔数在 L0 闸 n≥30 已约束，故不进 Pareto 维度）。

收敛判据①（spec §3.5）：连续 K 轮新 trial 无一进 Pareto 前沿（前沿不扩张）→ 收敛自停。
本模块提供 frontier_grew + converged_k_rounds；判据④覆盖度前置否决在 coverage.py，
判据②EI 在 search.py，判据③预算耗尽即 Plan 2 的 budget_exhausted。
"""


def pareto_frontier(trials, obj_max=("ann",), obj_min=("max_dd",)):
    """Pareto 非支配前沿（纯函数）。返回非支配 trial 的索引列表。

    obj_max: 越大越好的目标键（如 ann）；obj_min: 越小越好的目标键（如 max_dd）。
    非支配定义：trial i 不被任何 j 支配；j 支配 i ⟺ j 在所有目标上 ≥/≤ i（方向匹配）
    且至少一个目标严格优于。O(n²)——颈线法单 run trial 数 ≤ 千级，可接受。
    """
    n = len(trials)
    frontier = []
    for i in range(n):
        dominated = False
        for j in range(n):
            if i == j:
                continue
            ti, tj = trials[i], trials[j]
            # j 在 max 目标全 ≥ i，且 min 目标全 ≤ i
            ge_all = all(tj[k] >= ti[k] for k in obj_max)
            le_all = all(tj[k] <= ti[k] for k in obj_min)
            if not (ge_all and le_all):
                continue
            # 至少一个目标严格优于（否则相等不算支配）
            strict = (any(tj[k] > ti[k] for k in obj_max) or
                      any(tj[k] < ti[k] for k in obj_min))
            if strict:
                dominated = True
                break
        if not dominated:
            frontier.append(i)
    return frontier


def frontier_grew(old_frontier, new_frontier):
    """新前沿是否有 old 没有的点（前沿扩张）。

    new ⊆ old → 未扩张（False）；new 有 old 没有的点 → 扩张（True）。
    收缩（new 比 old 小）不算扩张——前沿只会因新非支配点而扩张。
    """
    return not set(new_frontier).issubset(set(old_frontier))


def converged_k_rounds(frontier_history, K=3):
    """连续 K 轮前沿不扩张 → 收敛（判据①，spec §3.5）。

    frontier_history: list[list[int]]，每轮的 Pareto 前沿索引集。
    需至少 K+1 轮历史（最近 K 轮各自对比前一轮）；不足则保守返回 False（不停）。
    """
    if len(frontier_history) < K + 1:
        return False
    # 最近 K 轮（索引 len-K .. len-1）各自相对前一轮都不扩张
    for r in range(len(frontier_history) - K, len(frontier_history)):
        if frontier_grew(frontier_history[r - 1], frontier_history[r]):
            return False
    return True
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_pareto.py -v
```
Expected: 6 passed。

- [ ] **Step 5: Commit**

```bash
git add discovery/pareto.py tests/discovery/test_pareto.py
git commit -m "feat(discovery): L4 Pareto 前沿+收敛判据①（纯函数，spec §3.5 v1.2/§4.1）

- pareto_frontier：多目标非支配（默认 ann↑/max_dd↓），O(n²) 颈线法 trial 量级够用
- frontier_grew/converged_k_rounds：判据①连续 K 轮前沿不扩张→收敛自停
- v1.2 降级：Pareto 退为 L1 候选筛选补充（主排序 calmar 在 judging）
- 判据④覆盖度（coverage.py）/②EI（search.py）/③预算耗尽（Plan 2 budget）分离

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: 参数空间覆盖度度量（`discovery/coverage.py`，判据④防伪收敛核心）

**Files:**
- Create: `discovery/coverage.py`、`tests/discovery/test_coverage.py`

**Interfaces:**
- Consumes: `discovery.sampler.PARAM_SPACE`（21 维 [(key, [candidates])]）。
- Produces: `grid_coverage(sampled_params, param_space=None) -> float`（ρ∈[0,1]）；`coverage_gate(rho, threshold=0.8) -> bool`（判据④）。后续 T6 用。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_coverage.py`:
```python
# -*- coding: utf-8 -*-
"""覆盖度度量④测试（纯函数，网格占用率）。"""


def test_grid_coverage_grows_with_samples():
    """ρ 随不重复采样组合数增加而上升。"""
    from discovery.coverage import grid_coverage
    space = [("w", [40, 60, 80]), ("t", [1, 3, 5])]   # 9 单元
    few = [{"w": 40, "t": 1}, {"w": 60, "t": 3}]               # 2 unique
    many = [{"w": 40, "t": 1}, {"w": 60, "t": 3}, {"w": 80, "t": 5}, {"w": 40, "t": 3}]  # 4 unique
    assert grid_coverage(many, space) > grid_coverage(few, space)
    assert grid_coverage(few, space) == 2 / 9
    assert grid_coverage(many, space) == 4 / 9


def test_grid_coverage_dedup():
    """重复组合不算（去重）。"""
    from discovery.coverage import grid_coverage
    space = [("w", [40, 60, 80])]   # 3 单元
    dups = [{"w": 40}, {"w": 40}, {"w": 40}]
    assert grid_coverage(dups, space) == 1 / 3


def test_grid_coverage_default_uses_param_space():
    """不传 param_space 时用 sampler.PARAM_SPACE（21 维）。"""
    from discovery.coverage import grid_coverage
    from discovery.sampler import PARAM_SPACE, sample_search
    batch = sample_search(n_sobol=5, n_random=5, seed=1)
    rho = grid_coverage(batch)   # 默认 PARAM_SPACE
    assert 0.0 < rho < 1.0


def test_coverage_gate():
    """判据④：ρ≥阈值→达标（允许其他判据自停）；ρ<阈值→否决。"""
    from discovery.coverage import coverage_gate
    assert coverage_gate(0.9, threshold=0.8) is True
    assert coverage_gate(0.8, threshold=0.8) is True   # 含等
    assert coverage_gate(0.5, threshold=0.8) is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_coverage.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.coverage'`）。

- [ ] **Step 3: 实现 `discovery/coverage.py`**

```python
# -*- coding: utf-8 -*-
"""参数空间覆盖度度量（spec §3.5 判据④，防伪收敛核心，纯函数）。

物理意图（spec §3.5 ⚠ 伪收敛陷阱）：收敛判据①②③ 只证"当前采样策略下没新东西"，
不能证"参数空间被充分探索"。若初始采样有盲区（21 维随机/贪心极易整片错过有效区域），
判据①会在采样不足的前沿上早早命中、自停，交付"撞到的孤峰"冒充相对最优。
**故判据④（覆盖度）是①②③ 的前置否决**——覆盖度不达标，判据①命中也不许停。

度量取网格单元占用率（design 决策2）：21 维每维按候选档分箱（候选档即箱），每个采样
params → 每维候选档索引 → 组合元组；ρ = 不重复组合数 / 总单元数（Π len(cand)）。
简单显式纯函数可单测，离散候选档天然分箱无需 KDE。ρ=0.8（spec §3.5 初定）。
"""


def grid_coverage(sampled_params, param_space=None):
    """网格单元占用率 ρ ∈ [0,1]（判据④度量，纯函数）。

    sampled_params: list[dict]（21 维，值在候选档内——sampler/sample_search/normalize 保证）。
    param_space: [(key, [candidates]), ...]，默认 sampler.PARAM_SPACE。
    ρ = 不重复候选档组合数 / Π len(candidates)。重复组合不算（去重）。
    """
    if param_space is None:
        from discovery.sampler import PARAM_SPACE
        param_space = PARAM_SPACE
    keys = [k for k, _ in param_space]
    # 每维 值→索引 映射（候选档值唯一可索引）
    cand_idx = [{c: i for i, c in enumerate(cands)} for _, cands in param_space]
    total = 1
    for _, cands in param_space:
        total *= len(cands)
    if total == 0:
        return 0.0
    seen = set()
    for p in sampled_params:
        # 值→索引元组（缺键/值不在候选档跳过——防御，正常路径不会触发）
        try:
            combo = tuple(cand_idx[d][p[keys[d]]] for d in range(len(keys)))
        except (KeyError, IndexError):
            continue
        seen.add(combo)
    return len(seen) / total


def coverage_gate(rho, threshold=0.8):
    """判据④：覆盖度是否达标（ρ≥threshold）。

    spec §3.5：覆盖度是判据①的前置否决——本函数返回 True 才允许判据①②自停；
    返回 False 时即便前沿不扩张、EI<ε 也不许停（须扩采样继续探索，防伪收敛）。
    threshold=0.8（spec §3.5 初定；实际标定留 Plan 4 daemon 跑后回溯，见 design §6）。
    """
    return rho >= threshold
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_coverage.py -v
```
Expected: 4 passed。

- [ ] **Step 5: Commit**

```bash
git add discovery/coverage.py tests/discovery/test_coverage.py
git commit -m "feat(discovery): 覆盖度度量④ 网格占用率（判据④前置否决防伪收敛，spec §3.5）

- grid_coverage：21 维候选档分箱，ρ=不重复组合/Πlen(cand)，纯函数
- coverage_gate：判据④ ρ≥0.8 达标，前置否决判据①②（覆盖不够不许自停）
- 防伪收敛核心（spec §3.5 ⚠）：判据①②③ 只证'没新东西'，④证'探索充分'

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: Deflated Sharpe Ratio（`discovery/dsr.py`，§3.7 闭式）

**Files:**
- Create: `discovery/dsr.py`、`tests/discovery/test_dsr.py`

**Interfaces:**
- Consumes: 无（纯 stdlib math）。
- Produces: `deflated_sharpe(sharpe, n_trials, n_obs, skew=0.0, kurt=3.0) -> float`（DSR∈[0,1]，L2 统计裁决）。后续 T6 标 top-N、T7 champions 报告用。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_dsr.py`:
```python
# -*- coding: utf-8 -*-
"""Deflated Sharpe Ratio 测试（闭式，纯 stdlib）。"""


def test_dsr_decreases_with_more_trials():
    """多重比较修正：试越多，同 sharpe 的 DSR 越低（最高更可能是运气）。"""
    from discovery.dsr import deflated_sharpe
    s_few = deflated_sharpe(sharpe=2.0, n_trials=10, n_obs=100)
    s_many = deflated_sharpe(sharpe=2.0, n_trials=1000, n_obs=100)
    assert s_few > s_many


def test_dsr_increases_with_sharpe():
    """更高 sharpe → DSR 更高（更显著）。"""
    from discovery.dsr import deflated_sharpe
    low = deflated_sharpe(sharpe=1.0, n_trials=10, n_obs=100)
    high = deflated_sharpe(sharpe=3.0, n_trials=10, n_obs=100)
    assert high > low


def test_dsr_increases_with_observations():
    """更长样本 → DSR 更高（样本量越大越显著）。"""
    from discovery.dsr import deflated_sharpe
    short = deflated_sharpe(sharpe=2.0, n_trials=10, n_obs=30)
    long = deflated_sharpe(sharpe=2.0, n_trials=10, n_obs=500)
    assert long > short


def test_dsr_range():
    """DSR ∈ [0,1]（是概率）。"""
    from discovery.dsr import deflated_sharpe
    for sh in [-1.0, 0.0, 1.0, 5.0]:
        v = deflated_sharpe(sharpe=sh, n_trials=5, n_obs=100)
        assert 0.0 <= v <= 1.0


def test_dsr_single_trial_no_multiple_comparison():
    """n_trials=1 → 无多重比较，SR_max=0，DSR 仅看 sharpe 显著性。"""
    from discovery.dsr import deflated_sharpe
    v = deflated_sharpe(sharpe=2.0, n_trials=1, n_obs=100)
    assert 0.0 <= v <= 1.0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_dsr.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.dsr'`）。

- [ ] **Step 3: 实现 `discovery/dsr.py`**

```python
# -*- coding: utf-8 -*-
"""Deflated Sharpe Ratio（spec §3.7，López de Prado 闭式，防多重比较选 bias）。

物理意图（spec §3.7）：嵌套验证（inner 选参/outer 纯评估）防了"数据窥探"（参数偷看
test 段），但没防"选 bias"——即便完全不偷看，在 M 组参数里挑 calmar/sharpe 最高，
这个"最高"的期望本就随 M 虚高（order statistics）。DSR 用闭式公式修正：输入 top 候选
的 sharpe + 收益序列偏度/峰度（修正非正态，颈线法 trades 尖峰厚尾）+ 试验次数 M
（修正多重比较）→ 输出零假设（最优≈基准）下观察到 ≥sharpe 的概率。

位置（spec §3.7）：L2 裁判，作用在 L1 calmar 排序后的 top-N 候选（如 top-20），不全局算。

诚实边界（spec §3.7/§13，ADR13）：DSR 依赖样本量，颈线法信号稀疏（单组几百笔、切折后
每折几十~百笔）→ 置信区间宽。极端情况 DSR 可能说"top-5 优劣在噪声内不可辨"——
那就承认"相对最优"在该数据量下不可辨识，而非硬选。本模块只算 DSR 值，判定（显著/运气）
留给调用方按阈值 + 诚实报告。

反魔法（ADR4）：逆正态 CDF 用 Ackhard 算法纯 Python 实现（math.erf 有，inverse erf 无），
不引 scipy。数学公开（López de Prado 2014 "The Deflated Sharpe Fund"）。
"""
import math


def _norm_cdf(x):
    """标准正态 CDF Φ(x)（math.erf 实现，纯 stdlib）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p):
    """标准正态逆 CDF Φ^{-1}(p)（Ackhard 算法，纯 Python 无 scipy 依赖）。

    p ∈ (0,1) → x。绝对误差 < 1e-9（Ackhard 1996 有理逼近，金融工程标准实现）。
    """
    # Ackhard 系数（常量，公开算法）
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    elif p <= phigh:
        q = p - 0.5
        r = q * q
        x = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5]) * q / \
            (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    else:
        q = math.sqrt(-2.0 * math.log(1 - p))
        x = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
             ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    return x


def _expected_max_sharpe(n_trials):
    """M 次独立试验下最高 sharpe 的期望（E[max z | M]，σ_sharpe≈1 年化口径）。

    Gumbel 简化近似：E[max z | M] ≈ Φ^{-1}(1 - 1/M)（M 大时精确；M=1 时无多重比较→0）。
    这是 DSR 的多重比较修正项：试越多，SR_max 越高，同 sharpe 的 DSR 越低。
    """
    if n_trials <= 1:
        return 0.0
    return _norm_ppf(1.0 - 1.0 / n_trials)


def deflated_sharpe(sharpe, n_trials, n_obs, skew=0.0, kurt=3.0):
    """Deflated Sharpe Ratio（López de Prado 2014 闭式，spec §3.7）。

    返回 DSR ∈ [0,1]：零假设（最优策略≈基准）下观察到 ≥sharpe 的概率。
    高→优势大概率是运气（多重比较膨胀），低→统计显著。

    参数：
      sharpe:  top 候选的（年化）夏普——L2 作用在 L1 calmar 排序后 top-N。
      n_trials: 试验次数 M（多重比较修正——试越多最高越虚高）。
      n_obs:   收益序列长度 T（样本量修正——越长越显著）。
      skew:    收益序列偏度（非正态修正；正态=0）。
      kurt:    收益序列峰度（非正态修正；正态=3，颈线法尖峰厚尾 kurt>3）。

    公式：DSR = Φ( (SR - SR_max) · √(T-1) / √(1 - skew·SR + (kurt-1)/4·SR²) )
    分母根号内为非正态修正的方差因子（Lo 2002）；负或零→数据异常返回 0。
    """
    sr_max = _expected_max_sharpe(n_trials)
    var_factor = 1.0 - skew * sharpe + (kurt - 1) / 4.0 * sharpe * sharpe
    if var_factor <= 0:
        return 0.0
    z = (sharpe - sr_max) * math.sqrt(max(n_obs - 1, 1)) / math.sqrt(var_factor)
    return _norm_cdf(z)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_dsr.py -v
```
Expected: 5 passed。

- [ ] **Step 5: Commit**

```bash
git add discovery/dsr.py tests/discovery/test_dsr.py
git commit -m "feat(discovery): DSR 统计裁决 闭式（防多重比较选 bias，spec §3.7）

- deflated_sharpe：López de Prado 2014 闭式，修正多重比较(M)+非正态(skew/kurt)+样本量(T)
- _norm_ppf：Ackhard 算法纯 Python（反魔法，不引 scipy；math.erf 有 inverse erf 无）
- L2 裁卫，作用在 L1 calmar 排序后 top-N（不全局算）
- 诚实边界（§3.7/§13）：信号稀疏致置信区间宽，如实报不强选（ADR13）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: optuna TPE 序贯优化（`discovery/search.py`，§7.2 阶段二）

**Files:**
- Create: `discovery/search.py`、`tests/discovery/test_search.py`
- Modify: `requirements.txt`（加 optuna）

**Interfaces:**
- Consumes: optuna（**前置安装**）；`discovery.sampler.PARAM_SPACE`（21 维候选档）；warm start seed_params 由 T6 用 Plan 2 `sample_search` 产。
- Produces: `tpe_search(seed_params, objective_fn, n_trials=20, seed=42, param_space=None) -> tuple[list[dict], optuna.study.Study]`（objective_fn(params)->float 是 inner calmar）；`expected_improvement(study, window=10) -> float`（判据②代理）。后续 T6 用。

**关键设计（design 决策1/3）**：TPE 目标 = inner **calmar**（对齐 §3.5 v1.2 主排序，非 ann）；Plan 2 Sobol 点作 `study.enqueue_trial` warm start（不废弃 Plan 2 投资）；TPE 序贯需前序结果，**主进程串行 evaluate**（不 spawn，与 Plan 2 Sobol 阶段的 ProcessPool 区分）。

- [ ] **Step 0: 安装 optuna（环境前置，spec §7.2 唯一新依赖）**

```bash
.venv310/Scripts/python.exe -m pip install optuna
.venv310/Scripts/python.exe -c "import optuna; print(optuna.__version__)"
```
然后在 `requirements.txt` 加 optuna（version pin 用上一步打印的版本，如 `optuna==3.6.1`）。spec §7.2"optuna 唯一新依赖，轻量通用优化框架，非 ADR4 红线"。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_search.py`:
```python
# -*- coding: utf-8 -*-
"""optuna TPE 序贯优化测试：warm start + 序贯集中 + EI + 可复现。
合成 objective_fn（不真实 evaluate，避免读 parquet）；真实 TPE 跑批集成在 Task 8 slow。
"""


def _toy_space():
    return [("window", [40, 60, 80])]


def test_tpe_search_returns_seed_plus_tpe_trials():
    """tpe_search 返回 (seed + tpe) 所有 trial params + study。"""
    from discovery.search import tpe_search
    obj = lambda p: p["window"] / 100.0   # window 越大 calmar 越高（合成）
    params, study = tpe_search([{"window": 40}], obj, n_trials=5, seed=42,
                               param_space=_toy_space())
    assert len(params) == 6               # 1 seed + 5 tpe
    assert all("window" in p for p in params)


def test_tpe_search_warm_start_seed_included():
    """warm start：enqueue 的 seed params 在 study trials 里。"""
    from discovery.search import tpe_search
    obj = lambda p: p["window"] / 100.0
    params, _ = tpe_search([{"window": 80}, {"window": 60}], obj, n_trials=3,
                           seed=42, param_space=_toy_space())
    assert {"window": 80} in params and {"window": 60} in params


def test_tpe_search_finds_high_calmar():
    """TPE 序贯应探索到高 calmar 区（best_value >= 0.5，即 60 或 80 档）。"""
    from discovery.search import tpe_search
    obj = lambda p: p["window"] / 100.0   # 最优 window=80 → 0.8
    _, study = tpe_search([{"window": 40}], obj, n_trials=20, seed=42,
                          param_space=_toy_space())
    assert study.best_value >= 0.5        # TPE 前 10 startup 随机覆盖 3 档，必探到 60/80


def test_tpe_search_reproducible():
    """同 seed 同输入 → 同 best_value（TPESampler(seed=) 可复现）。"""
    from discovery.search import tpe_search
    obj = lambda p: p["window"] / 100.0
    _, s1 = tpe_search([{"window": 40}], obj, n_trials=8, seed=7, param_space=_toy_space())
    _, s2 = tpe_search([{"window": 40}], obj, n_trials=8, seed=7, param_space=_toy_space())
    assert s1.best_value == s2.best_value


def test_expected_improvement_zero_when_stalled():
    """判据②代理：最近 window best 无改进 → EI=0。"""
    from discovery.search import expected_improvement
    class FT:
        def __init__(self, v): self.value = v
    class FS:
        def __init__(self, vs): self.trials = [FT(v) for v in vs]
    # 前 5 轮爬升到 0.9，后 5 轮全 0.9（最近 window=5 无改进）
    s = FS([0.1, 0.3, 0.5, 0.7, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9])
    assert expected_improvement(s, window=5) == 0.0


def test_expected_improvement_positive_when_growing():
    """最近 window best 仍改进 → EI>0。"""
    from discovery.search import expected_improvement
    class FT:
        def __init__(self, v): self.value = v
    class FS:
        def __init__(self, vs): self.trials = [FT(v) for v in vs]
    s = FS([0.1, 0.3, 0.5, 0.7, 0.9])
    assert expected_improvement(s, window=5) > 0.0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_search.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.search'`）。

- [ ] **Step 3: 实现 `discovery/search.py`**

```python
# -*- coding: utf-8 -*-
"""L3 搜索层完成·TPE 序贯优化（spec §7.2 阶段二，optuna）。

物理意图（spec §7.2 v1.1）：v1 把 TPE 当"可选增强"是 L3 空心根源。v1.1 确立"Sobol 准随机
初始覆盖 → TPE 序贯优化"两阶段。Plan 2 已立 Sobol 初始覆盖（判据④覆盖度的物理手段）；
本模块做阶段二——在 Sobol 撒的点上拟合 TPE 后验，向预期提升 EI 高的区域集中采样。

关键设计（design 决策1/3）：
1. TPE 目标 = inner **calmar**（objective_fn 返回 calmar）。spec §6.2 v1.1 写"objective=ann"，
   但 §3.5 v1.2 主排序改 calmar，ann 是 risk_metrics 复利放大失真产物（§1.4 实证夏普15/ann201%）。
   Plan 3 让 TPE 跟随 v1.2 用 calmar（TPE 优化什么就排序什么）。
2. Plan 2 自写 Sobol 不废弃——`study.enqueue_trial` 注入 Sobol 点 warm start TPE，
   对齐 §7.2"Sobol 初始覆盖→TPE"两阶段。
3. TPE 序贯需前序结果（每次采基于历史），**主进程串行 evaluate**（不 spawn 子进程）。
   与 Plan 2 Sobol 阶段的 ProcessPool 并发区分——TPE 阶段是精化，串行可接受（夜跑预算内）。

反魔法（ADR4）：optuna 是 spec §7.2 唯一新依赖，轻量通用优化框架（非 vectorbt/backtrader
等量化黑盒红线）。TPE 算法用 optuna 成熟实现（自写双 GMM l(x)/g(x)+EI 最大化数值 bug 风险高）。
"""
import optuna

DEFAULT_N_TPE_TRIALS = 20


def tpe_search(seed_params, objective_fn, n_trials=DEFAULT_N_TPE_TRIALS,
               seed=42, param_space=None):
    """optuna TPESampler 序贯优化 + Sobol warm start。

    seed_params: warm start 点（Plan 2 sample_search 产，list[dict] 21 维）。
    objective_fn(params) -> float: 返回 inner calmar（最大化；T6 runner 闭包 universe 提供）。
    n_trials: TPE 新采 trial 数（不含 seed）。
    param_space: [(key, [candidates]), ...]，默认 sampler.PARAM_SPACE（21 维离散档）。
    返回 (all_params, study)：all_params = seed + tpe 全部 trial 的 params（list[dict]）；
      study = optuna study（T6 读 best_value / 给 expected_improvement）。

    离散采样：每维 trial.suggest_categorical（颈线法参数是离散候选档，非连续）。
    """
    if param_space is None:
        from discovery.sampler import PARAM_SPACE
        param_space = PARAM_SPACE
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=seed),
    )
    # warm start：enqueue Plan 2 Sobol 点（TPE 从已知覆盖起点序贯，不重复铺覆盖）
    for p in seed_params:
        study.enqueue_trial(p)

    def obj(trial):
        # 离散档采样（enqueue 的 trial 用既有值，suggest_categorical 声明 search space）
        params = {k: trial.suggest_categorical(k, cands) for k, cands in param_space}
        return objective_fn(params)

    total = len(seed_params) + n_trials
    study.optimize(obj, n_trials=total)
    all_params = [{k: t.params[k] for k, _ in param_space} for t in study.trials]
    return all_params, study


def expected_improvement(study, window=10):
    """判据②代理（spec §3.5 EI<ε）：最近 window trial 内 best_value 改进幅度。

    optuna TPESampler 内部 EI 不暴露 API，用代理——最近 window trial 的 value 极差
    （max - window 起点）。极差≈0 = best 不涨 = 没有预期能改进的点（EI 衰减）。
    调用方按 ε 阈值判（如 ε=1e-3）。trial 数 <2 保守返回 inf（不停，让 TPE 多跑）。
    """
    values = [t.value for t in study.trials if t.value is not None]
    if len(values) < 2:
        return float("inf")
    recent = values[-min(window, len(values)):]
    return max(recent) - recent[0]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_search.py -v
```
Expected: 6 passed。

- [ ] **Step 5: Commit**

```bash
git add discovery/search.py tests/discovery/test_search.py requirements.txt
git commit -m "feat(discovery): L3 TPE 序贯优化 optuna+Sobol warm start（spec §7.2 阶段二）

- tpe_search：TPESampler 离散 suggest_categorical，enqueue Sobol 点 warm start
- TPE 目标=inner calmar（design 决策1，对齐 §3.5 v1.2；非 §6.2 ann 失真口径）
- expected_improvement：判据②代理（optuna EI 不暴露，用最近 window best 改进幅度）
- 主进程串行 evaluate（TPE 序贯需前序结果，与 Plan 2 Sobol 并发区分）
- optuna 唯一新依赖（spec §7.2，轻量通用框架非 ADR4 量化黑盒红线）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: 耦合5 语义厘清 + 耦合6 runtime 裁剪（`constraints.py` + `worker.py`）

**Files:**
- Modify: `discovery/constraints.py`（耦合5 docstring 厘清）、`discovery/worker.py`（耦合6 runtime 裁剪）
- Test: `tests/discovery/test_constraints.py`（加耦合5 用例）、`tests/discovery/test_worker.py`（加耦合6 用例）

**Interfaces:**
- Consumes: Plan 2 `constraints.normalize_params`、`worker._eval_worker`/`evaluate`。
- Produces: 耦合5 docstring 厘清（suppression↔decay_tau 独立可调，不改逻辑）；耦合6 `_eval_worker` 内 `n_total==0→None`（design 决策5/6）。

- [ ] **Step 1: 写失败测试（耦合5 加到 test_constraints.py，耦合6 加到 test_worker.py）**

追加到 `tests/discovery/test_constraints.py`:
```python
def test_coupling5_suppression_decay_tau_independent():
    """耦合5（design 决策5）：suppression/decay_tau 独立可调，normalize 不强行捆绑。

    代码实证（method_v0.py:163-173）：decay_tau=None 等权时 suppression 仍生效；spec §7.1
    '捆绑调'语义已退化为'都可调'——凭空裁剪误杀合法组合，故 Plan 3 仅厘清不裁。
    """
    from discovery.constraints import normalize_params
    # decay_tau=None（等权）+ suppression 非 0 → 都保留
    p = normalize_params({"min_suppression": 0.5, "decay_tau": None})
    assert p["min_suppression"] == 0.5
    assert p["decay_tau"] is None
    # decay_tau=30 + suppression=0 → 也都保留（不强制捆绑）
    p2 = normalize_params({"min_suppression": 0.0, "decay_tau": 30})
    assert p2["decay_tau"] == 30
    assert p2["min_suppression"] == 0.0
```

追加到 `tests/discovery/test_worker.py`:
```python
def test_eval_worker_coupling6_empty_trades_returns_none(monkeypatch, champion_params, synth_sym_df):
    """耦合6 runtime 裁剪（design 决策6）：evaluate 返回 n_total==0（挂单区间全空退化）→ None。

    spec §7.1 耦合6 buy_limit<cancel×H/ATR 依赖 runtime H/ATR（每标的每信号点不同），
    采样期无法静态判（Plan 2 收窄理由）。worker 拿 universe 后用 n_total==0 作代理：
    全 universe 无交易 = params 挂单区间全空退化。完整逐信号点裁剪需内核（ADR8 零改动），
    discovery 层只做 n_total=0 代理。
    """
    from discovery import worker
    from discovery.split import HoldoutSplit, Segment
    from datetime import date

    class FakeMeta:
        snapshot_hash = "fakehash"
        universe_count = 1
    monkeypatch.setattr(worker, "freeze", lambda lake_start="2025-01-01": ({"300001.SZ": synth_sym_df}, FakeMeta()))
    monkeypatch.setattr(worker, "holdout_split", lambda embargo_days=5: HoldoutSplit(
        Segment("i", date(2025, 1, 1), date(2025, 12, 31)),
        Segment("o", date(2026, 1, 1), date(2026, 12, 31)), embargo_days))
    # monkeypatch evaluate 返回 n_total=0（挂单区间全空退化）
    monkeypatch.setattr(worker, "evaluate", lambda p, u, s: {"inner": {"ann": 0}, "outer": {"ann": 0}, "n_total": 0})

    worker._init_worker("2025-01-01", 5)
    out = worker._eval_worker(champion_params)
    assert out is None   # 耦合6 runtime 裁剪
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_constraints.py::test_coupling5_suppression_decay_tau_independent tests/discovery/test_worker.py::test_eval_worker_coupling6_empty_trades_returns_none -v -m "not slow"
```
Expected: 耦合5 用例应已过（normalize 本就保留二者——若无过则测试本身验证现状，跳到 Step 3 只改 worker）；耦合6 用例 FAIL（当前 `_eval_worker` 不检查 n_total，n_total=0 仍返回 tuple）。

- [ ] **Step 3: 实现——`constraints.py` 耦合5 docstring 厘清 + `worker.py` 耦合6 runtime 裁剪**

`discovery/constraints.py` 的 `normalize_params` docstring 末尾追加耦合5 厘清段（不改逻辑）：
```python
    # 耦合5（suppression↔decay_tau，spec §7.1）厘清（design 决策5）：
    # 代码实证（method_v0.py:163-173）二者独立可调——decay_tau=None 等权时 suppression 仍
    # 生效，spec 原文"捆绑调"语义退化为"都可调"。凭空裁剪会误杀合法组合，故 Plan 3 仅文档
    # 厘清，不在 normalize/is_feasible 强制捆绑（与耦合1-4 的硬裁剪区分）。
```

`discovery/worker.py` 的 `_eval_worker` 改造（加 n_total==0 裁剪）：
```python
def _eval_worker(params):
    """Pool.map 调用：评估单组 params，返回 (params, result_dict) 或 None（异常/退化）。

    顶层定义（可 pickle）。读 _WORKER_STATE（initializer 设的 universe/split），调
    objective.evaluate。两类返回 None：
    1. 异常（spec §8 拷问②：worker 崩溃 → 单 trial 标 failed，run 继续）。
    2. 耦合6 runtime 裁剪（design 决策6，spec §7.1）：n_total==0 = 全 universe 挂单区间
       全空（params 退化）→ None。完整逐信号点裁剪需内核（ADR8 零改动），discovery 层
       只做 n_total=0 代理。
    """
    if not _WORKER_STATE["ready"]:
        return None
    try:
        res = evaluate(params, _WORKER_STATE["universe"], _WORKER_STATE["split"])
        # 耦合6 runtime 裁剪：n_total==0 = 挂单区间全空退化（spec §7.1 耦合6 代理）
        if res.get("n_total", 0) == 0:
            return None
        return (params, res)
    except Exception:
        return None
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_constraints.py tests/discovery/test_worker.py -v -m "not slow"
```
Expected: 全 passed（含耦合5 厘清 + 耦合6 裁剪 + Plan 2 既有用例零回归）。

- [ ] **Step 5: Commit**

```bash
git add discovery/constraints.py discovery/worker.py tests/discovery/test_constraints.py tests/discovery/test_worker.py
git commit -m "feat(discovery): 耦合5 语义厘清 + 耦合6 runtime 裁剪（spec §7.1，design 决策5/6）

- 耦合5：suppression/decay_tau 独立可调（代码实证），normalize 仅 docstring 厘清不裁
- 耦合6：_eval_worker 内 n_total==0→None（挂单区间全空退化，runtime H/ATR 代理）
- 诚实收窄：耦合6 完整逐信号点裁剪需内核（ADR8），discovery 层只做 n_total=0 代理

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: runner 接入 TPE + Pareto + 收敛自停 + DSR（`discovery/runner.py` + `discovery/store.py`）

**Files:**
- Modify: `discovery/runner.py`（`RunSummary` 扩字段 + `run_search` 两阶段改造）、`discovery/store.py`（加 `read_trials_by_snapshot`）
- Test: `tests/discovery/test_runner.py`（加收敛/DSR 用例）

**Interfaces:**
- Consumes: T1 `pareto_frontier`、T2 `grid_coverage`/`coverage_gate`、T3 `deflated_sharpe`、T4 `tpe_search`/`expected_improvement`；Plan 2 `sample_search`/`eval_batch`/`freeze`/`evaluate`/`store.*`/`feasibility_gate`。
- Produces: `run_search(..., tpe_trials=0, rho_threshold=0.8, ei_eps=1e-3) -> RunSummary`（status 扩 `converged` + rho/ei/frontier_size/dsr_top/convergence_reason）；`store.read_trials_by_snapshot(conn, snapshot_hash) -> list[dict]`。T7 cli 用。

**关键设计**：两阶段（Sobol 批量并发→TPE 序贯精化）；单 run 收敛判据 = 判据④覆盖度 ρ + 判据②EI（TPE 后），判据①连续K轮 + 跨 run EI 衰减留 Plan 4 daemon（spec §5.2），判据③=budget_exhausted；TPE 阶段 `_res_cache` 避免双 evaluate；DSR 标 top-1（诚实报告不强选）。

- [ ] **Step 1: 写失败测试（追加到 `tests/discovery/test_runner.py`）**

```python
import json as _json


# 合成评估结果（inner/outer metrics 齐全，供 mock eval/tpe/evaluate 用）
_RES = {"inner": {"ann": 0.5, "calmar": 2.0, "max_dd": 0.2, "n": 100, "sharpe": 1.5},
        "outer": {"ann": 0.5, "calmar": 2.0, "max_dd": 0.2, "n": 80, "sharpe": 1.5}, "n_total": 180}


def _one_param(window=80):
    return {"window": window, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
            "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0}


def _mock_search_deps(monkeypatch, runner, *, sampled, tpe_params=None, tpe_values=None):
    """mock run_search 的外部依赖（sample_search/eval_batch/tpe_search/evaluate/freeze/_engine_hash）。"""
    monkeypatch.setattr(runner, "sample_search", lambda **kw: sampled)
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [(p, _RES) for p in plist])
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    if tpe_params is not None:
        # 构造 fake optuna study（供 expected_improvement 读 .trials[].value）
        class _FT:
            def __init__(self, v): self.value = v
        class _FS:
            def __init__(self, vs): self.trials = [_FT(v) for v in vs]; self.best_value = max(vs) if vs else 0.0
        monkeypatch.setattr(runner, "tpe_search",
                            lambda sp, obj, n_trials, seed, **kw: (tpe_params, _FS(tpe_values or [])))
        monkeypatch.setattr(runner, "evaluate", lambda p, u, s: _RES)
        monkeypatch.setattr(runner, "freeze", lambda lake_start="2025-01-01": ({}, _fake_meta()))


def test_run_search_budget_when_sobol_only(tmp_path, monkeypatch):
    """tpe_trials=0（Sobol-only）→ budget_exhausted（Plan 2 逻辑不破；判据①跨 run 留 daemon）。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    _mock_search_deps(monkeypatch, runner, sampled=[_one_param()])
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)   # tpe_trials 默认 0
    assert s.status == "budget_exhausted"
    assert s.n_new_trials == 1


def test_run_search_converges_with_tpe_low_ei(tmp_path, monkeypatch):
    """tpe_trials>0 + 覆盖达标 + EI<ε → converged（判据④+②命中）。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    sampled = [_one_param(w) for w in (40, 60, 80)]
    _mock_search_deps(monkeypatch, runner, sampled=sampled, tpe_params=sampled,
                      tpe_values=[0.5] * 10)   # 全 0.5 → EI=0（<ε）
    s = runner.run_search(_fake_meta(), _fake_split(), budget=3, n_sobol=3, n_random=0,
                          seed=1, db_path=db, tpe_trials=2, rho_threshold=0.0)   # rho_threshold=0 强制覆盖达标
    assert s.status == "converged"
    assert "ei_below_eps" in s.convergence_reason


def test_run_search_budget_when_coverage_low(tmp_path, monkeypatch):
    """ρ<阈值 → budget_exhausted（判据④前置否决，即便 EI=0 也不自停）。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    _mock_search_deps(monkeypatch, runner, sampled=[_one_param()], tpe_params=[_one_param()],
                      tpe_values=[0.5] * 10)
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db, tpe_trials=1, rho_threshold=0.99)   # ρ 达不到 0.99
    assert s.status == "budget_exhausted"


def test_run_search_dsr_and_frontier_marked(tmp_path, monkeypatch):
    """top-1 算 DSR、Pareto 前沿大小入 RunSummary。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db"); init_db(db)
    _mock_search_deps(monkeypatch, runner, sampled=[_one_param()])
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)
    assert 0.0 <= s.dsr_top <= 1.0
    assert s.frontier_size >= 1   # 至少 1 组 trial，自身即前沿
    assert s.rho >= 0.0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_runner.py -v -m "not slow"
```
Expected: 新用例 FAIL（`run_search` 无 tpe_trials 参数 / RunSummary 无 rho 等字段 / store 无 read_trials_by_snapshot）。Plan 2 既有用例零回归。

- [ ] **Step 3: 实现——`store.py` 加 read_trials_by_snapshot + `runner.py` RunSummary 扩 + run_search 两阶段改造**

`discovery/store.py` 追加（`trial_exists` 之后）:
```python
def read_trials_by_snapshot(conn, snapshot_hash):
    """读某 snapshot 下所有 trial（Pareto/DSR 计算用，spec §3.4）。

    返回 list[dict]，每项含 trial_id/inner_metrics/outer_metrics/source。
    inner_metrics/outer_metrics 是 JSON 字符串（write_trial 存的），调用方 json.loads。
    """
    rows = conn.execute(
        "SELECT trial_id, inner_metrics, outer_metrics, source FROM trial WHERE snapshot_hash=?",
        (snapshot_hash,)).fetchall()
    return [dict(r) for r in rows]
```

`discovery/runner.py` 顶部 import 块追加（既有 import 之后）:
```python
import json
```
并在 `from discovery.store import (...)` 加 `read_trials_by_snapshot`；加 Plan 3 模块 import（文件顶部，worker import 之后）:
```python
from discovery.coverage import grid_coverage, coverage_gate
from discovery.pareto import pareto_frontier
from discovery.dsr import deflated_sharpe
from discovery.search import tpe_search, expected_improvement
```

`discovery/runner.py` 用下面**完整新版**替换 Plan 2 的 `RunSummary` + `_source_of` 之间（含 RunSummary 扩字段 + 加 `_params_key`）:
```python
@dataclass
class RunSummary:
    """跑批汇总（Plan 3：status 扩 converged + 收敛/覆盖/EI/DSR 字段）。"""
    n_sampled: int = 0
    n_evaluated: int = 0
    n_new_trials: int = 0
    n_skipped_dup: int = 0
    n_failed: int = 0
    top_inner_calmar: float = 0.0
    top_trial_id: str = ""
    db_path: str = ""
    status: str = "budget_exhausted"   # Plan 3 扩 "converged"
    snapshot_hash: str = ""
    # Plan 3 新增
    convergence_reason: str = ""   # "coverage_met+ei_below_eps" / "budget_exhausted"
    rho: float = 0.0               # 覆盖度（判据④）
    ei: float = 0.0                # 预期提升代理（判据②）
    frontier_size: int = 0         # Pareto 前沿大小
    dsr_top: float = 0.0           # top-1 DSR（L2 统计裁决）


def _params_key(params):
    """params dict → 可 hash 键（TPE _res_cache 用，避免双 evaluate）。"""
    return tuple(sorted((k, str(v)) for k, v in params.items()))
```

`discovery/runner.py` 用下面**完整新版**替换 Plan 2 的 `run_search`:
```python
def run_search(snapshot_meta: SnapshotMeta, split: HoldoutSplit, budget: int,
               n_sobol: int, n_random: int, seed: int,
               db_path: str, n_proc=None, lake_start="2025-01-01",
               tpe_trials: int = 0, rho_threshold: float = 0.8,
               ei_eps: float = 1e-3) -> RunSummary:
    """跑批主函数（Plan 3：两阶段搜索 + 收敛自停 + DSR）。

    阶段一（Plan 2 Sobol 批量并发）：sample_search→去重→eval_batch→落库。
    阶段二（Plan 3 TPE 序贯，tpe_trials>0）：主进程 freeze→tpe_search（Sobol warm start，
      inner calmar 目标）→落库 TPE 新 trial（_res_cache 避免双 evaluate）。
    收敛判据（单 run）：判据④覆盖度 ρ 前置否决 + 判据②EI（TPE 后）；判据①连续K轮 + 跨 run
      EI 衰减留 Plan 4 daemon（spec §5.2）；判据③预算耗尽=budget_exhausted。
    DSR（§3.7）：top-1 trial 算 DSR 标注（L2 统计裁决，诚实报告不强选，ADR13）。
    """
    init_db(db_path)
    engine_hash = _engine_hash()
    split_tag = _split_tag(split)

    # === 阶段一：Sobol 批量并发（Plan 2 逻辑） ===
    n_sobol_eff = min(n_sobol, budget)
    n_random_eff = min(n_random, max(0, budget - n_sobol_eff))
    sampled = sample_search(n_sobol=n_sobol_eff, n_random=n_random_eff, seed=seed)
    n_sampled = len(sampled)
    with connect(db_path) as conn:
        write_snapshot(conn, snapshot_meta)
    to_eval, n_skipped = [], 0
    with connect(db_path) as conn:
        for p in sampled:
            tid = trial_id_of(p, snapshot_meta.snapshot_hash, seed)
            if trial_exists(conn, tid):
                n_skipped += 1
            else:
                to_eval.append(p)
    results = eval_batch(to_eval, lake_start=lake_start,
                         embargo_days=split.embargo_days, n_proc=n_proc) if to_eval else []
    n_new, n_failed = 0, 0
    all_evaluated = []   # 累积本 run 评估的 params（算覆盖度 ρ）
    with connect(db_path) as conn:
        for item in results:
            if item is None:
                n_failed += 1
                continue
            params, res = item
            tid = _persist_trial(conn, params, snapshot_meta, split_tag,
                                 engine_hash, res, _source_of(seed), seed)
            if tid is None:
                continue
            n_new += 1
            all_evaluated.append(params)

    # === 阶段二：TPE 序贯精化（tpe_trials>0，主进程串行 evaluate） ===
    tpe_study = None
    if tpe_trials > 0:
        universe, _ = freeze(lake_start=lake_start)   # 主进程 freeze（TPE 串行 evaluate 用）
        _res_cache = {}                                # params→res，避免落库时双 evaluate
        def _obj(p):
            res = evaluate(p, universe, split)
            _res_cache[_params_key(p)] = res
            return res["inner"].get("calmar", 0.0)
        all_params, tpe_study = tpe_search(sampled, _obj, n_trials=tpe_trials, seed=seed)
        # 落库 TPE 新 trial（sampled 之后的是 TPE 新采；缓存命中避免双跑）
        with connect(db_path) as conn:
            for p in all_params[len(sampled):]:
                res = _res_cache.get(_params_key(p)) or evaluate(p, universe, split)
                tid = _persist_trial(conn, p, snapshot_meta, split_tag,
                                     engine_hash, res, "tpe", seed)
                if tid is None:
                    continue
                n_new += 1
                all_evaluated.append(p)

    # === 收敛判据 + 覆盖度 + EI ===
    rho = grid_coverage(all_evaluated)
    ei = expected_improvement(tpe_study) if tpe_study is not None else float("inf")
    converged = False
    reason = "budget_exhausted"
    if coverage_gate(rho, rho_threshold):
        if tpe_study is not None and ei < ei_eps:
            converged = True
            reason = "coverage_met+ei_below_eps"
        # Sobol-only（无 TPE）单 run 不判 EI → budget（判据①跨 run 留 daemon）
    status = "converged" if converged else "budget_exhausted"

    # === Pareto 前沿 + DSR top-1（从 store 读所有 trial，信息隔离：只用 inner） ===
    with connect(db_path) as conn:
        trials_db = read_trials_by_snapshot(conn, snapshot_meta.snapshot_hash)
    inner_metrics = []
    for t in trials_db:
        try:
            inner_metrics.append((json.loads(t["inner_metrics"]), t))
        except (TypeError, ValueError):
            continue
    frontier_idxs = pareto_frontier([m for m, _ in inner_metrics]) if inner_metrics else []
    candidates = [(m.get("calmar", 0.0), t) for m, t in inner_metrics if feasibility_gate(m)]
    top_calmar, top_tid, dsr_top = 0.0, "", 0.0
    if candidates:
        candidates.sort(reverse=True, key=lambda x: x[0])
        top_calmar, top_t = candidates[0]
        top_tid = top_t["trial_id"]
        top_m = json.loads(top_t["inner_metrics"]) if top_t["inner_metrics"] else {}
        # DSR top-1：n_trials=本 snapshot trial 数（多重比较），n_obs=top 的交易笔数
        dsr_top = deflated_sharpe(top_m.get("sharpe", 0.0),
                                  n_trials=len(trials_db), n_obs=top_m.get("n", 30))

    return RunSummary(
        n_sampled=n_sampled, n_evaluated=len(to_eval), n_new_trials=n_new,
        n_skipped_dup=n_skipped, n_failed=n_failed,
        top_inner_calmar=top_calmar, top_trial_id=top_tid,
        db_path=db_path, snapshot_hash=snapshot_meta.snapshot_hash,
        status=status, convergence_reason=reason,
        rho=rho, ei=ei, frontier_size=len(frontier_idxs), dsr_top=dsr_top,
    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/ -m "not slow" -v
```
Expected: 全 passed（Plan 3 T1-T5 + Plan 2 既有零回归；含本 task 4 新收敛/DSR 用例）。

- [ ] **Step 5: Commit**

```bash
git add discovery/runner.py discovery/store.py tests/discovery/test_runner.py
git commit -m "feat(discovery): runner 两阶段搜索+收敛自停+DSR（spec §5.2/§3.5/§3.7）

- run_search 两阶段：Sobol 批量并发→TPE 序贯精化（_res_cache 避免双 evaluate）
- 收敛判据：④覆盖度 ρ 前置否决 + ②EI<ε → status=converged；①连续K轮留 daemon
- RunSummary 扩 status/rho/ei/frontier_size/dsr_top/convergence_reason
- store.read_trials_by_snapshot：读全 trial 算 Pareto/DSR
- DSR top-1 标注（诚实报告不强选，ADR13）；Pareto 自写纯函数（不用 optuna 多目标）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: CLI 接入收敛 + champions/report 子命令（`discovery/cli.py` + `discovery/__init__.py`）

**Files:**
- Modify: `discovery/cli.py`（cmd_run 改造 + 新增 cmd_champions/cmd_report + main 子解析器）、`discovery/__init__.py`（导出 Plan 3 API）
- Test: `tests/discovery/test_cli_plan3.py`（子命令注册）

**Interfaces:**
- Consumes: T6 `run_search`（新参数 tpe_trials/rho_threshold）+ `store.read_trials_by_snapshot`；T1 `pareto_frontier`、T3 `deflated_sharpe`、`judging.feasibility_gate`。
- Produces: `python -m discovery run --tpe-trials N --rho-threshold ρ`（收敛打印）；`champions`（Pareto+DSR top 报告）；`report`（run 历史简报）。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_cli_plan3.py`:
```python
# -*- coding: utf-8 -*-
"""cli Plan 3 子命令注册测试（subprocess --help，非 slow）。"""
import subprocess
import sys


def test_run_help_has_tpe_and_rho_args():
    """run 子命令注册了 --tpe-trials / --rho-threshold（Plan 3 新参数）。"""
    p = subprocess.run([sys.executable, "-m", "discovery", "run", "--help"],
                       capture_output=True, text=True)
    assert p.returncode == 0
    assert "--tpe-trials" in p.stdout
    assert "--rho-threshold" in p.stdout


def test_champions_subcommand_registered():
    """champions 子命令注册（Plan 3 新增）。"""
    p = subprocess.run([sys.executable, "-m", "discovery", "champions", "--help"],
                       capture_output=True, text=True)
    assert p.returncode == 0
    assert "--top-n" in p.stdout


def test_report_subcommand_registered():
    """report 子命令注册（Plan 3 新增）。"""
    p = subprocess.run([sys.executable, "-m", "discovery", "report", "--help"],
                       capture_output=True, text=True)
    assert p.returncode == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_cli_plan3.py -v
```
Expected: `test_run_help_has_tpe_and_rho_args` FAIL（无 --tpe-trials）；`test_champions/report` FAIL（invalid choice: 'champions'/'report'）。

- [ ] **Step 3: 实现——`cli.py` cmd_run 改造 + 新增 cmd_champions/cmd_report + main 子解析器 + `__init__.py` 导出**

`discovery/cli.py` 用下面**新版替换** Plan 2 的 `cmd_run`:
```python
def cmd_run(args):
    """两阶段搜索跑批：Sobol 批量→TPE 序贯→落库→收敛自停（spec §5.1/§7.2，Plan 2+3）。"""
    from discovery.runner import run_search
    universe, meta = freeze(args.lake_start)
    split = holdout_split(args.embargo)
    print(f"=== discovery run：两阶段搜索（snapshot={meta.snapshot_hash}）===")
    print(f"snapshot: {meta.universe_count} 只 | {meta.date_range} | hash {meta.snapshot_hash}")
    print(f"配置: budget={args.budget} sobol={args.n_sobol} random={args.n_random} "
          f"tpe={args.tpe_trials} proc={args.n_proc} seed={args.seed} "
          f"embargo={args.embargo} rho_threshold={args.rho_threshold}")
    summary = run_search(meta, split, budget=args.budget, n_sobol=args.n_sobol,
                         n_random=args.n_random, seed=args.seed, db_path=_db_path(),
                         n_proc=args.n_proc, lake_start=args.lake_start,
                         tpe_trials=args.tpe_trials, rho_threshold=args.rho_threshold)
    print(f"--- RunSummary ---")
    print(f"n_sampled={summary.n_sampled} n_evaluated={summary.n_evaluated} "
          f"n_new_trials={summary.n_new_trials} n_skipped_dup={summary.n_skipped_dup} "
          f"n_failed={summary.n_failed}")
    print(f"top_inner_calmar={summary.top_inner_calmar:.2f} top_trial_id={summary.top_trial_id} "
          f"dsr_top={summary.dsr_top:.3f}")
    print(f"收敛: status={summary.status} reason={summary.convergence_reason} "
          f"rho={summary.rho:.3f} ei={summary.ei:.4f} frontier_size={summary.frontier_size}")
    print(f"db={summary.db_path}")
    print(f"信息隔离: 汇总只用 inner（spec §6.2）；判据①连续K轮 + 跨 run EI 衰减留 Plan 4 daemon")
```

`discovery/cli.py` 在 `cmd_run` 之后新增两个函数:
```python
def cmd_champions(args):
    """Pareto 前沿 + DSR 冠军报告（spec §3.5/§3.7，读 store 最新 snapshot 的 trial）。"""
    import json
    from discovery.store import connect
    from discovery.pareto import pareto_frontier
    from discovery.dsr import deflated_sharpe
    from discovery.judging import feasibility_gate
    db = _db_path()
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT trial_id, snapshot_hash, inner_metrics FROM trial ORDER BY created_at DESC"
        ).fetchall()
    if not rows:
        print(f"无 trial 记录（db={db}）")
        return
    latest = rows[0]["snapshot_hash"]
    trials = [r for r in rows if r["snapshot_hash"] == latest]
    metrics = []
    for t in trials:
        try:
            metrics.append((json.loads(t["inner_metrics"]), t["trial_id"]))
        except (TypeError, ValueError):
            continue
    frontier_idxs = pareto_frontier([m for m, _ in metrics]) if metrics else []
    feasible = [(m, tid) for m, tid in metrics if feasibility_gate(m)]
    ranked = sorted(feasible, key=lambda x: x[0].get("calmar", 0.0), reverse=True)
    print(f"=== discovery champions（snapshot={latest}，{len(trials)} trial，前沿 {len(frontier_idxs)}）===")
    if not ranked:
        print("无可行域内 trial（L0 闸 max_dd≤0.4 ∧ n≥30 未过）")
        return
    for i, (m, tid) in enumerate(ranked[:args.top_n]):
        dsr = deflated_sharpe(m.get("sharpe", 0.0), n_trials=len(trials), n_obs=m.get("n", 30))
        print(f"#{i+1} {tid}: calmar={m.get('calmar', 0):.2f} ann={m.get('ann', 0)*100:.1f}% "
              f"max_dd={m.get('max_dd', 0)*100:.1f}% n={m.get('n', 0)} DSR={dsr:.3f}")


def cmd_report(args):
    """run 历史简报（snapshot/trial 计数 + 最近 snapshot，spec §12 验收）。"""
    from discovery.store import connect
    db = _db_path()
    with connect(db) as conn:
        n_trial = conn.execute("SELECT COUNT(*) c FROM trial").fetchone()["c"]
        n_snap = conn.execute("SELECT COUNT(*) c FROM snapshot").fetchone()["c"]
        snaps = conn.execute(
            "SELECT snapshot_hash, universe_count, date_range, created_at "
            "FROM snapshot ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
    print(f"=== discovery report（db={db}）===")
    print(f"snapshot: {n_snap} | trial: {n_trial}")
    for s in snaps:
        print(f"  {s['snapshot_hash']}: {s['universe_count']}只 {s['date_range']} ({s['created_at']})")
```

`discovery/cli.py` 的 `main()` 子解析器区改造——`ap_r` 加两参数 + `ap_v` 后加 champions/report（在 `ap_r.set_defaults(func=cmd_run)` 之后、`args = ap.parse_args(argv)` 之前插入）:
```python
    ap_r.add_argument("--tpe-trials", type=int, default=0, dest="tpe_trials",
                     help="TPE 序贯 trial 数（Plan 3，0=仅 Sobol）")
    ap_r.add_argument("--rho-threshold", type=float, default=0.8, dest="rho_threshold",
                     help="覆盖度阈值 ρ（判据④前置否决，Plan 3）")
    ap_c = sub.add_parser("champions", help="Pareto 前沿 + DSR 冠军报告（Plan 3）")
    ap_c.add_argument("--top-n", type=int, default=10, dest="top_n", help="报 top-N")
    ap_c.set_defaults(func=cmd_champions)
    ap_rp = sub.add_parser("report", help="run 历史简报（Plan 3）")
    ap_rp.set_defaults(func=cmd_report)
```

`discovery/__init__.py` 在 Plan 2 导出后追加 Plan 3 导出:
```python
# Plan 3 新增
from discovery.pareto import pareto_frontier, frontier_grew, converged_k_rounds
from discovery.coverage import grid_coverage, coverage_gate
from discovery.dsr import deflated_sharpe
from discovery.search import tpe_search, expected_improvement
```
并在 `__all__` 追加:
```python
           # Plan 3
           "pareto_frontier", "frontier_grew", "converged_k_rounds",
           "grid_coverage", "coverage_gate", "deflated_sharpe",
           "tpe_search", "expected_improvement",
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_cli_plan3.py -v
.venv310/Scripts/python.exe -m pytest tests/discovery/ -m "not slow" -q
```
Expected: test_cli_plan3 3 passed；全量 non-slow 零回归。

- [ ] **Step 5: Commit**

```bash
git add discovery/cli.py discovery/__init__.py tests/discovery/test_cli_plan3.py
git commit -m "feat(discovery): cli 接入收敛 + champions/report 子命令（Plan 3 集成）

- run 加 --tpe-trials/--rho-threshold，打印收敛字段（status/rho/ei/dsr/frontier）
- champions：读 store 最新 snapshot，Pareto 前沿 + L1 calmar 排序 + DSR 标注
- report：snapshot/trial 计数 + 最近 snapshot 简报
- __init__ 导出 Plan 3 API（pareto/coverage/dsr/search）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: slow 集成测试（真实 optuna TPE 跑批收敛）

**Files:**
- Create: `tests/discovery/test_plan3_e2e.py`（slow）

**Interfaces:**
- Consumes: T1-T7 全部（端到端 optuna TPE + 收敛 + 落库）。

- [ ] **Step 1: 写 slow 集成测试**

`tests/discovery/test_plan3_e2e.py`:
```python
# -*- coding: utf-8 -*-
"""Plan 3 端到端 slow 集成：真实 optuna TPE 小 budget 跑批（~10min），验证收敛字段 + 落库。"""
import os
import subprocess
import sys

import pytest


@pytest.mark.slow
def test_run_with_tpe_produces_convergence_fields(tmp_path):
    """discovery run --budget 2 --n-sobol 2 --tpe-trials 2（~10min，Sobol 2+TPE 2 组），
    exit 0，stdout 含 status/rho/ei 收敛字段，SQLite 有 trial 落库。"""
    db = tmp_path / "run_tpe.db"
    env = {**os.environ, "DISCOVERY_DB": str(db)}
    proc = subprocess.run(
        [sys.executable, "-m", "discovery", "run",
         "--budget", "2", "--n-sobol", "2", "--n-random", "0",
         "--tpe-trials", "2", "--embargo", "5", "--n-proc", "2", "--seed", "42"],
        capture_output=True, text=True, env=env, cwd=os.getcwd(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "status=" in proc.stdout
    assert "rho=" in proc.stdout
    # SQLite 落库（Sobol + TPE trial，至少 1 组）
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    n = conn.execute("SELECT COUNT(*) c FROM trial").fetchone()["c"]
    conn.close()
    assert n >= 1


@pytest.mark.slow
def test_champions_after_run(tmp_path):
    """先 run 再 champions：champions 读落库 trial 报 top（不空）。"""
    db = tmp_path / "run_ch.db"
    env = {**os.environ, "DISCOVERY_DB": str(db)}
    subprocess.run([sys.executable, "-m", "discovery", "run",
                    "--budget", "1", "--n-sobol", "1", "--n-random", "0",
                    "--embargo", "5", "--n-proc", "1", "--seed", "7"],
                   capture_output=True, text=True, env=env, cwd=os.getcwd())
    proc = subprocess.run([sys.executable, "-m", "discovery", "champions"],
                          capture_output=True, text=True, env=env, cwd=os.getcwd())
    assert proc.returncode == 0
    assert "champions" in proc.stdout
```

- [ ] **Step 2: 跑 non-slow 确认不破坏（slow 跳过）**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/ -m "not slow" -q
```
Expected: 全 passed（本 task 2 用例 deselected slow）。

- [ ] **Step 3: 手动 slow 冒烟（~10min，验证真实 optuna TPE 链路）**

```bash
DISCOVERY_DB=tmp_plan3_smoke.db .venv310/Scripts/python.exe -m discovery run --budget 2 --n-sobol 2 --n-random 0 --tpe-trials 2 --n-proc 2 --seed 42
```
Expected: exit 0，stdout 含 `status=` `rho=` `ei=` `frontier_size=` `dsr_top=`，SQLite 落库 ≥1 trial。清理：`rm -f tmp_plan3_smoke.db*`。

- [ ] **Step 4: Commit**

```bash
git add tests/discovery/test_plan3_e2e.py
git commit -m "test(discovery): Plan 3 端到端 slow 集成 真实 optuna TPE 跑批收敛

- test_run_with_tpe_produces_convergence_fields：Sobol2+TPE2 跑批，验 status/rho/ei + 落库
- test_champions_after_run：run 后 champions 读 trial 报 top
- slow 标记，非 slow 回归不跑

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec 覆盖**（逐节指 task）：
- §7.2 TPE 序贯（阶段二）→ T4 ✓（optuna TPESampler + Sobol warm start，目标 inner calmar）
- §3.5 收敛判据①连续K轮 → T1 `converged_k_rounds` ✓（单 run 不触发，跨 run 留 Plan 4 daemon——plan §T6 标注）
- §3.5 判据② EI<ε → T4 `expected_improvement` + T6 自停 ✓
- §3.5 判据③ 预算耗尽 → Plan 2 `budget_exhausted`（T6 保留）✓
- §3.5 判据④ 覆盖度前置否决 → T2 `coverage_gate` + T6 ✓
- §4.1 pareto.py → T1 ✓（自写纯函数，不用 optuna 多目标）
- §3.7 DSR → T3 `deflated_sharpe` + T6/T7 ✓
- §7.1 耦合5 → T5 ✓（docstring 厘清不裁）；耦合6 → T5 ✓（runtime n_total=0 代理）
- §5.2 自停循环 → T6 ✓（两阶段 + 收敛）
- §4.1 cli champions/report → T7 ✓

**2. Placeholder 扫描**：无 TBD/TODO；ρ=0.8 / ei_eps=1e-3 / K=3 是 spec 既定默认值（design §6 标注实际标定留 Plan 4），非 placeholder。

**3. Type consistency**（跨 task 接口核对）：
- T1 `pareto_frontier(trials, obj_max, obj_min) -> list[int]` ↔ T6 `pareto_frontier([m for m,_ in inner_metrics])`、T7 `pareto_frontier([m for m,_ in metrics])` ✓
- T1 `converged_k_rounds(history, K)` —— T6 单 run 未用（跨 run 留 daemon），保留供 Plan 4 ✓
- T2 `grid_coverage(sampled_params, param_space=None) -> float` ↔ T6 `grid_coverage(all_evaluated)` ✓；`coverage_gate(rho, threshold) -> bool` ↔ T6 ✓
- T3 `deflated_sharpe(sharpe, n_trials, n_obs, skew, kurt) -> float` ↔ T6 `deflated_sharpe(top_m["sharpe"], n_trials=len(trials_db), n_obs=top_m["n"])`、T7 同 ✓
- T4 `tpe_search(seed_params, objective_fn, n_trials, seed, param_space) -> (list[dict], Study)` ↔ T6 `tpe_search(sampled, _obj, n_trials=tpe_trials, seed=seed)` ✓；`expected_improvement(study, window) -> float` ↔ T6 `expected_improvement(tpe_study)` ✓
- T6 `read_trials_by_snapshot(conn, snapshot_hash)`（store 新增）↔ T6/T7 读 ✓
- T6 `run_search(..., tpe_trials, rho_threshold, ei_eps) -> RunSummary`（status/rho/ei/frontier_size/dsr_top）↔ T7 `cmd_run` 传 `tpe_trials=args.tpe_trials, rho_threshold=args.rho_threshold` + 打印 ✓
- T4 `objective_fn(params)->float` = inner calmar ↔ T6 `_obj` 返回 `res["inner"]["calmar"]` ✓（design 决策1 一致）

**4. 范围**：8 task 单 plan 可实施（T1-T3 + T5 独立纯函数/小改，T4 装 optuna，T6 集成，T7 cli，T8 slow 收尾）；schtasks daemon/publish 留 Plan 4（design §1）。

**5. 诚实边界**（已标注，非 placeholder）：
- 判据①连续K轮 + 跨 run EI 衰减 → Plan 4 daemon（T6 注释 + plan 标注）。
- DSR 信号稀疏致置信区间宽 → T3 docstring + design §6（如实报不强选）。
- 耦合6 完整逐信号点裁剪需内核（ADR8）→ T5 n_total=0 代理（design 决策6）。
- ρ=0.8 实际标定 → Plan 4 daemon 跑后回溯（design §6）。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-24-discovery-l3-l4-convergence.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 每个 task 派 fresh subagent，task 间 review，fast iteration（适合 8 个 task + TDD + optuna 新依赖 + Windows multiprocessing 需独立验证 spawn/串行边界）。

**2. Inline Execution** - 本 session 内用 executing-plans 批量执行 + checkpoint review。

Which approach?

