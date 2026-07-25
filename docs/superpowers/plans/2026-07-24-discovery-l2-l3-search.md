# 参数发现引擎 · L2 吞吐 + L3 搜索基础 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Plan 1 的单组 `objective.evaluate` 升级为**并发搜索**——约束裁剪砍废组合 + Sobol 准随机初始覆盖 + random 补充 + ProcessPool 并发跑批 + SQLite 断点续跑，产出 N 组参数的并发评估落库能力（TPE/Pareto/收敛判据留 Plan 3）。

**Architecture:** `constraints.py` 纯函数过滤 6 处耦合废组合（spec §7.1）；`sampler.py` 自写 Sobol 低差异序列（**不引 optuna，反魔法**）+ random 补充，约束裁剪后采样；`worker.py` 用标准 `multiprocessing.Pool`（spawn 兼容，顶层 `_init_worker`/`_eval_worker` 可 pickle，initializer 一次加载 snapshot universe 复用避免每 trial 重读 455MB parquet）；`runner.py` 跑批调度（采样→Pool 并发→落库 trial_id 去重→收敛判据占位）；`cli run` 命令串起全链。

**Tech Stack:** Python 3.10（`.venv310`）、`multiprocessing.Pool`（标准库，Windows spawn）、SQLite3（Plan 1 既有 WAL）、pandas、pytest。**零新增依赖**（Sobol 自写，optuna 留 Plan 3）。回测内核 `strategies/neckline/` 零改动。

## Global Constraints

- **同源内核**：复用 `strategies/neckline/backtest.py::scan_symbol` 与 `risk_metrics`（ADR8 零改动）；复用 Plan 1 已立的 `discovery/snapshot.freeze/load_universe`、`discovery/split.holdout_split`、`discovery/objective.evaluate/ID_KEYS/EXEC_KEYS`、`discovery/store.{init_db,connect,write_trial,write_snapshot,trial_id_of,trial_exists,DEFAULT_DB_PATH}`、`discovery/judging.{feasibility_gate,calmar_rank}`、`discovery/cli` 的 `_db_path`/`_engine_hash` 模式（**不重造 Plan 1 接口**）。
- **零反向依赖**：`discovery/` 不依赖 `trading/`（不实盘下单），只读 `strategies/neckline/` 内核。
- **反魔法**：**不引 optuna/vectorbt/qlib/backtrader**。Sobol 准随机序列**自写**（纯 Python，零新依赖，spec §7.2 显式允许"或自写"）；TPE/CMA-ES/NSGA-II 留 Plan 3。
- **Windows multiprocessing spawn**：worker 函数与 initializer 必须**顶层定义、可 pickle**（禁 lambda/闭包/嵌套函数/不可 pickle 参数）；`python -m discovery` 入口必须 `if __name__ == "__main__":` 守护（spawn 会重新 import 模块）；每个 worker 一次 `freeze` 加载 universe（复用，避免每 trial 重读 parquet）。
- **约束裁剪先于采样**：spec §7.1 六处耦合废组合**裁剪后**才采样（纯函数过滤器，零新增依赖）。
- **断点续跑去重**：`trial_id_of(params, snapshot_hash, seed)` 为去重键，`INSERT OR IGNORE`（Plan 1 store 已实现），kill/重启自动接续已落 trial 不重跑。
- **信息隔离**（Plan 1 已立）：搜索只用 `evaluate` 返回的 **inner** metrics（`feasibility_gate`/`calmar_rank`），outer 不反馈任何选择（spec §6.2）。
- **全中文注释**：所有新增代码像素级中文注释（CLAUDE.md）。
- **环境**：`.venv310/Scripts/python.exe`；cwd = repo root；`tests/conftest.py` 已配 `sys.path`（`import discovery/strategies` OK）；`pytest.ini` markers 含 `slow`；Plan 1 slow 集成测试已验证 `freeze()` ~5s + `evaluate` 单组 ~3min（真实 data_lake）。
- **Plan 2 范围限定**：L2 并发 + L3 约束裁剪 + Sobol/random 采样。**不做** TPE、Pareto 前沿、收敛判据（判据①连续K轮无新前沿 / 判据②EI<ε / 判据④覆盖度）、schtasks 守护、experiment 闭环——这些留 Plan 3/4/5。Plan 2 的"收敛判据"仅为**占位**（`runner` 跑完 budget 即停，返回汇总）。
- **反幻觉**：用标准 `multiprocessing.Pool`，**不用** spec 提及但未确认的 `backtest/worker.py`（grep 未验证其存在/接口）；Plan 2 worker 自包含。

---

## File Structure

| 文件 | 职责 | 创建/修改 |
|---|---|---|
| `discovery/constraints.py` | L3：6 处耦合废组合的纯函数过滤器 + `normalize_params`（trailing 互锁/min_rr 死参固定） | 创建 |
| `discovery/sampler.py` | L3：自写 Sobol 低差异序列 + random 补充；`sample_search(n_sobol, n_random, seed)` 约束裁剪后产合法 params 流 | 创建 |
| `discovery/worker.py` | L2：顶层 `_init_worker(lake_start)` initializer + 顶层 `_eval_worker(params)` 评估函数（Pool.map 可 pickle） | 创建 |
| `discovery/runner.py` | L2：`run_search(snapshot_meta, split, budget, n_sobol, n_random, n_proc, seed, db_path)` 跑批调度——采样→Pool→落库→断点续跑去重→汇总 | 创建 |
| `discovery/cli.py` | 加 `run` 子命令 | 修改（加 `cmd_run` + 子解析器） |
| `discovery/__init__.py` | 导出 Plan 2 新增 API | 修改 |
| `tests/discovery/test_constraints.py` | 6 处耦合裁剪 + normalize 单测 | 创建 |
| `tests/discovery/test_sampler.py` | Sobol 均匀性 + random + 约束裁剪后全合法 + 可复现 | 创建 |
| `tests/discovery/test_worker.py` | initializer 复用 + `_eval_worker` 顶层可 pickle + 单组评估返回结构 | 创建 |
| `tests/discovery/test_runner.py` | 断点续跑去重 + 采样落库计数 + 汇总字段（合成 universe 快测） | 创建 |
| `tests/discovery/test_cli_run.py` | `python -m discovery run --budget N` 集成（slow，真实 data_lake 小 budget） | 创建 |

**依赖顺序**：Task 1 (constraints) → Task 2 (sampler) → Task 3 (worker) → Task 4 (runner) → Task 5 (cli run + 集成)。每 task 产出独立可测交付物，前 task 测试通过才进下一 task。

---

## Task 1: 约束裁剪（`discovery/constraints.py`）

**Files:**
- Create: `discovery/constraints.py`、`tests/discovery/test_constraints.py`

**Interfaces:**
- Consumes: 无（纯函数，零外部依赖；`ID_KEYS`/`EXEC_KEYS` 概念与 `discovery/objective.py` 同源但本 task 自带常量，避免循环依赖）。
- Produces: `PARAM_KEYS`（21 维键名有序列表）；`normalize_params(params) -> dict`（trailing 互锁 grace=0 固定 step/floor、min_rr 死参数固定 2.0）；`is_feasible(params) -> bool`（6 处耦合裁剪判据）；`filter_feasible(params_iter) -> list[dict]`（约束裁剪后合法组合流）。

**物理意图**（spec §7.1）：21 维笛卡尔积里 6 处耦合组合物理无意义，纯函数裁剪掉再搜——零新增依赖，随机+贪心也更高效（不必第一刀上 optuna，ADR4 反魔法）。**6 处耦合**：
1. trailing 互锁（`grace=0` 时 `step/floor` 固定，不搜）；
2. `min_rr` 死参数（结构恒 `rr=2.0`，固定）；
3. `tp1_h_mult ≤ tp_h_mult`（防退化）；
4. `cancel_thresh_mult ≥ tp1_h_mult`（防过保守），`cancel=None` 视为放飞不撤（合法）；
5. `min_suppression` ↔ `decay_tau` 同开关（`decay_tau=None` 等权时 suppression 仍生效，二者独立调，无强制捆绑——spec §7.1 原文"二者捆绑调"在代码实证下退化为"都可调"，本 task 仅标注不强制裁剪）；
6. `buy_limit_atr_mult < cancel_thresh_mult×(H/ATR)`（挂单区间非空，H/ATR 动态依赖运行时数据，**采样期无法判定**，本 task 标注 N/A 留 Plan 3 runtime 裁剪）。

**裁剪决策（诚实标注）**：第 5、6 处因"同开关语义歧义"与"依赖 runtime 数据"，Plan 2 仅实现 1/2/3/4（可在采样期静态判定的四处）；5/6 留 Plan 3（runtime 裁剪 + 语义厘清）。这是对 spec §7.1 的**显式收窄**，非遗漏——避免凭空裁剪不确定的语义。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_constraints.py`:
```python
# -*- coding: utf-8 -*-
"""约束裁剪测试（spec §7.1）：纯函数，快测，零 data_lake 依赖。"""


def test_param_keys_21_dims():
    """PARAM_KEYS 覆盖 21 维（11 识别 + 10 执行含 trailing 3）。"""
    from discovery.constraints import PARAM_KEYS
    assert len(PARAM_KEYS) == 21
    for k in ["window", "min_rr", "tp1_h_mult", "tp_h_mult",
              "cancel_thresh_mult", "trailing_grace", "trailing_step", "trailing_floor"]:
        assert k in PARAM_KEYS


def test_normalize_min_rr_fixed():
    """min_rr 是死参数（结构恒 2.0），normalize 后强制 2.0（spec §7.1 耦合2）。"""
    from discovery.constraints import normalize_params
    p = normalize_params({"min_rr": 1.5, "window": 80})
    assert p["min_rr"] == 2.0
    assert p["window"] == 80   # 其它参数不动


def test_normalize_trailing_grace_zero_freezes_step_floor():
    """grace=0 时 trailing 关闭（固定止损基线），step/floor 固定不搜（spec §7.1 耦合1）。

    物理意图：simulate_exit 的 trailing 仅在 grace>0 AND step>0 激活；grace=0 等价
    固定止损（=当前 EXEC_DEFAULTS 基线），step/floor 取任何值都不生效——搜它们是白跑。
    """
    from discovery.constraints import normalize_params
    p = normalize_params({"trailing_grace": 0, "trailing_step": 0.15, "trailing_floor": 0.5})
    assert p["trailing_grace"] == 0
    assert p["trailing_step"] == 0.0      # 固定为基线（不生效值）
    assert p["trailing_floor"] == 0.0


def test_normalize_trailing_grace_positive_keeps_step_floor():
    """grace>0 时 trailing 激活，step/floor 保留搜索值。"""
    from discovery.constraints import normalize_params
    p = normalize_params({"trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.5})
    assert p["trailing_grace"] == 5
    assert p["trailing_step"] == 0.1
    assert p["trailing_floor"] == 0.5


def test_is_feasible_tp1_le_tp_h():
    """tp1_h_mult ≤ tp_h_mult（防退化，spec §7.1 耦合3）。"""
    from discovery.constraints import is_feasible
    assert is_feasible({"tp1_h_mult": 1.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 3.0,
                        "trailing_grace": 5, "trailing_step": 0.1}) is True
    assert is_feasible({"tp1_h_mult": 3.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 3.0,
                        "trailing_grace": 5, "trailing_step": 0.1}) is False


def test_is_feasible_cancel_ge_tp1():
    """cancel_thresh_mult ≥ tp1_h_mult（防过保守，spec §7.1 耦合4）；None=放飞不撤=合法。"""
    from discovery.constraints import is_feasible
    # cancel < tp1 → 非法（未到 tp1 就撤单 = 放弃突破）
    assert is_feasible({"tp1_h_mult": 1.5, "tp_h_mult": 2.5, "cancel_thresh_mult": 1.0,
                        "trailing_grace": 5, "trailing_step": 0.1}) is False
    # cancel ≥ tp1 → 合法
    assert is_feasible({"tp1_h_mult": 1.5, "tp_h_mult": 2.5, "cancel_thresh_mult": 2.0,
                        "trailing_grace": 5, "trailing_step": 0.1}) is True
    # cancel=None → 放飞不撤（颈线法默认语义）→ 合法
    assert is_feasible({"tp1_h_mult": 1.5, "tp_h_mult": 2.5, "cancel_thresh_mult": None,
                        "trailing_grace": 5, "trailing_step": 0.1}) is True


def test_is_feasible_uses_normalized_trailing():
    """is_feasible 在 grace=0 时不因 step/floor 报错（normalize 后这些值不参与判定）。"""
    from discovery.constraints import is_feasible
    # grace=0 + 任意 step/floor 都应合法（trailing 不生效）
    assert is_feasible({"tp1_h_mult": 1.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 3.0,
                        "trailing_grace": 0, "trailing_step": 0.99, "trailing_floor": 0.99}) is True


def test_filter_feasible_keeps_legal_drops_illegal():
    """filter_feasible 过滤整批：合法保留，非法丢弃，并先 normalize。"""
    from discovery.constraints import filter_feasible
    batch = [
        {"min_rr": 1.5, "tp1_h_mult": 1.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 3.0,
         "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0, "window": 80},  # 合法
        {"min_rr": 2.0, "tp1_h_mult": 3.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 3.0,
         "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0, "window": 60},  # 非法（tp1>tp_h）
        {"min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5, "cancel_thresh_mult": 0.5,
         "trailing_grace": 0, "trailing_step": 0.15, "trailing_floor": 0.5, "window": 40},  # 非法（cancel<tp1）
    ]
    kept = filter_feasible(batch)
    assert len(kept) == 1
    assert kept[0]["min_rr"] == 2.0          # normalize 已固定死参数
    assert kept[0]["window"] == 80
    # 第3条 grace=0 本应被 normalize 救回 trailing，但 cancel<tp1 仍非法 → 被滤
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_constraints.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.constraints'`）。

- [ ] **Step 3: 实现 `discovery/constraints.py`**

```python
# -*- coding: utf-8 -*-
"""L3 约束裁剪（spec §7.1，纯函数过滤器，零新增依赖，ADR4 反魔法）。

物理意图：21 维笛卡尔积里有 6 处物理无意义的耦合组合（trailing 互锁 / 死参数 / 退化 / 冲突 /
同开关 / 挂单区间空）。裁剪掉再搜——既省算力（不白跑废组合），也让随机/Sobol 采样更高效
（合法密度提升），是不第一刀上 optuna 的前提（spec §7.2）。

Plan 2 实现范围（诚实收窄）：
- 耦合 1（trailing 互锁）：normalize_params 强制 grace=0 时 step/floor 固定 ✓
- 耦合 2（min_rr 死参数）：normalize_params 强制 min_rr=2.0（method_v0 结构恒 rr=2.0）✓
- 耦合 3（tp1 ≤ tp_h）：is_feasible 静态判定 ✓
- 耦合 4（cancel ≥ tp1，None=放飞合法）：is_feasible 静态判定 ✓
- 耦合 5（suppression ↔ decay_tau 同开关）：**Plan 2 不裁剪**——代码实证二者独立可调
  （decay_tau=None 等权时 suppression 仍生效），spec §7.1 原文"捆绑调"语义在实证下退化为
  "都可调"，凭空裁剪会误杀合法组合。留 Plan 3 语义厘清后再定。
- 耦合 6（buy_limit < cancel×H/ATR 挂单区间非空）：**Plan 2 不裁剪**——H/ATR 是 runtime
  数据（每标的每信号点不同），采样期无法静态判定。留 Plan 3 runtime 裁剪（worker 内判）。
"""
# 21 维参数键（与 discovery/objective.ID_KEYS+EXEC_KEYS 同源；本模块自带避免循环依赖）
PARAM_KEYS = [
    # 识别层 11 维
    "window", "min_touches", "min_suppression", "local_extrema_window",
    "min_bottoms", "breakout_vol_mult", "min_rr", "max_h_atr",
    "stop_atr_mult", "tp_h_mult", "decay_tau",
    # 执行层 7 维
    "max_holding", "max_wait", "cooldown", "buy_limit_atr_mult",
    "tp1_h_mult", "tp1_portion", "cancel_thresh_mult",
    # trailing 3 维
    "trailing_grace", "trailing_step", "trailing_floor",
]

# 耦合2：min_rr 是死参数（method_v0.py 结构恒 rr=2.0，调它无意义），固定为 2.0
DEAD_MIN_RR = 2.0
# 耦合1：grace=0 时 trailing 不激活，step/floor 固定为基线（取任何值都不生效）
TRAILING_OFF_STEP = 0.0
TRAILING_OFF_FLOOR = 0.0


def normalize_params(params):
    """规范化参数：固定死参数 + trailing 互锁处置。

    - min_rr 强制 DEAD_MIN_RR（耦合2，结构恒定，搜它无意义）。
    - trailing_grace=0 时 step/floor 强制 OFF 基线（耦合1，trailing 不生效，搜它们白跑）。
    - trailing_grace>0 时 step/floor 保留（trailing 激活，搜索有效）。
    返回新 dict（不改原 params，纯函数）。
    """
    p = dict(params)
    p["min_rr"] = DEAD_MIN_RR
    if p.get("trailing_grace", 0) == 0:
        p["trailing_step"] = TRAILING_OFF_STEP
        p["trailing_floor"] = TRAILING_OFF_FLOOR
    return p


def is_feasible(params):
    """静态可行性判定（耦合 3/4）。params 应已 normalize。

    - 耦合3：tp1_h_mult ≤ tp_h_mult（防 tp1>tp_h 退化——止盈1 比止盈2 还远无意义）。
    - 耦合4：cancel_thresh_mult ≥ tp1_h_mult（防 cancel<tp1 过保守——未到 tp1 就撤单
      等于放弃突破）；cancel=None 视为放飞不撤（颈线法默认语义），合法。
    trailing 在 normalize 后已处置，此处不判（grace=0 时 step/floor 不参与判定）。
    """
    tp1 = params.get("tp1_h_mult", 0)
    tp_h = params.get("tp_h_mult", 0)
    if tp1 > tp_h:
        return False
    cancel = params.get("cancel_thresh_mult", None)
    if cancel is not None and cancel < tp1:
        return False
    return True


def filter_feasible(params_iter):
    """约束裁剪整批：normalize → is_feasible → 保留合法组合。

    采样层（sampler）产出原始 params 流后调本函数裁剪，再送 worker 评估。
    返回 list（已 normalize），顺序与输入一致（合法项原序保留）。
    """
    out = []
    for p in params_iter:
        np_ = normalize_params(p)
        if is_feasible(np_):
            out.append(np_)
    return out
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_constraints.py -v
```
Expected: 8 passed。

- [ ] **Step 5: Commit**

```bash
git add discovery/constraints.py tests/discovery/test_constraints.py
git commit -m "feat(discovery): L3 约束裁剪 纯函数过滤 4/6 处耦合废组合（spec §7.1）

- normalize_params：trailing 互锁 grace=0 固定 step/floor + min_rr 死参固定 2.0
- is_feasible：tp1≤tp_h（防退化）+ cancel≥tp1（防过保守，None=放飞合法）
- filter_feasible：normalize→is_feasible 整批裁剪，零新增依赖（ADR4 反魔法）
- 诚实收窄：耦合5（suppression↔decay_tau 语义歧义）/耦合6（runtime H/ATR）留 Plan 3

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: 采样层（`discovery/sampler.py`，自写 Sobol + random）

**Files:**
- Create: `discovery/sampler.py`、`tests/discovery/test_sampler.py`

**Interfaces:**
- Consumes: `discovery/constraints.{PARAM_KEYS, filter_feasible}`（Task 1）；`scripts/param_iter.PARAM_SPACE`（候选档，复用同源不重造）。
- Produces: `PARAM_SPACE`（21 维候选档，从 `discovery.tools.param_iter` import 复用）；`sobol_sample(dim, n, seed) -> np.ndarray`（自写 Sobol，shape=(n, dim)，值∈[0,1)）；`random_sample(dim, n, seed) -> np.ndarray`；`_scale_to_candidates(unit_vec, candidates_per_dim) -> dict`（单位向量 → 每维候选档索引 → params dict）；`sample_search(n_sobol, n_random, seed, n_attempts_factor=3) -> list[dict]`（约束裁剪后产合法 params 流， Sobol 初始覆盖 + random 补充）。

**关键设计**：
- **自写 Sobol**（反魔法，零新依赖）：用 Joe & Kuo (2008) 标准方向数实现，支持 dim≤21（覆盖 21 维参数空间）。Sobol 序列值∈[0,1)^dim，低差异（比纯随机均匀，spec §7.2/§3.5 判据④覆盖度的物理手段）。**确定性**：同 seed 同 dim 同 n → 同输出（可复现，落 trial.seed）。
- **候选档离散化**：单位向量 ×候选档数 → 索引 → 取 `PARAM_SPACE` 对应档（与 `scripts/param_iter.PARAM_SPACE` 同源，复用不重造）。Sobol 在离散档上仍是低差异覆盖（比纯随机聚集少）。
- **约束裁剪后采样**：`sample_search` 采样 N×factor 候选 → `filter_feasible` 裁剪 → 取前 N 合法（factor=3 兜底，废组合密度约 1/3-1/2，3× 通常够；不够则随机补）。
- **可 pickle**：`sample_search` 返回 list[dict]（纯 Python 类型），可跨进程传递。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_sampler.py`:
```python
# -*- coding: utf-8 -*-
"""采样层测试：Sobol 均匀性 + random + 约束裁剪后全合法 + 可复现（快测，零 data_lake）。"""
import numpy as np
import pytest


def test_sobol_shape_and_range():
    """Sobol 输出 shape=(n, dim)，值∈[0,1)。"""
    from discovery.sampler import sobol_sample
    s = sobol_sample(dim=5, n=16, seed=42)
    assert s.shape == (16, 5)
    assert (s >= 0).all() and (s < 1).all()


def test_sobol_deterministic():
    """同 seed 同 dim 同 n → 同输出（可复现，落 trial.seed 的基石）。"""
    from discovery.sampler import sobol_sample
    a = sobol_sample(dim=4, n=8, seed=7)
    b = sobol_sample(dim=4, n=8, seed=7)
    assert np.array_equal(a, b)


def test_sobol_seed_advances():
    """不同 seed → 不同序列（seed 真起作用，非固定起点）。"""
    from discovery.sampler import sobol_sample
    a = sobol_sample(dim=4, n=8, seed=1)
    b = sobol_sample(dim=4, n=8, seed=2)
    assert not np.array_equal(a, b)


def test_sobol_uniformity_better_than_random():
    """Sobol 一维投影离散度 ≤ 纯随机（低差异序列，spec §3.5 判据④覆盖度物理手段）。

    判据：把 [0,1) 分 8 桶，Sobol 一维投影的桶计数方差应 ≤ 纯随机（均匀覆盖）。
    这是 spec §3.5 "判据④ Sobol 覆盖均匀性 ≥ 纯随机" 的最小可测化。
    """
    from discovery.sampler import sobol_sample, random_sample
    n = 64
    sob = sobol_sample(dim=3, n=n, seed=42)[:, 0]    # 第一维投影
    rnd = random_sample(dim=3, n=n, seed=42)[:, 0]
    bins = np.linspace(0, 1, 9)   # 8 桶
    sob_counts = np.histogram(sob, bins)[0]
    rnd_counts = np.histogram(rnd, bins)[0]
    assert sob_counts.std() <= rnd_counts.std() + 1e-9   # Sobol 桶计数方差 ≤ 随机


def test_random_sample_shape():
    from discovery.sampler import random_sample
    r = random_sample(dim=6, n=10, seed=1)
    assert r.shape == (10, 6)
    assert (r >= 0).all() and (r < 1).all()


def test_scale_to_candidates_picks_valid_levels():
    """单位向量 → 候选档索引 → 取 PARAM_SPACE 对应档（值在候选列表内）。"""
    from discovery.sampler import _scale_to_candidates, PARAM_SPACE
    # 单位向量接近 0 → 第一档；接近 1 → 最后一档
    unit_vec = np.array([0.0, 0.99])
    candidates_per_dim = [
        [40, 60, 80],            # window 3 档
        [None, 30, 60],          # decay_tau 3 档（含 None）
    ]
    p = _scale_to_candidates(unit_vec, candidates_per_dim)
    assert p[0] == 40             # 0.0 → 第一档
    assert p[1] == 60             # 0.99 → 最后一档


def test_sample_search_all_feasible():
    """sample_search 产出全部合法（经 filter_feasible，tp1≤tp_h + cancel≥tp1）。"""
    from discovery.sampler import sample_search
    from discovery.constraints import is_feasible
    batch = sample_search(n_sobol=20, n_random=10, seed=42)
    assert len(batch) >= 10       # 至少裁剪后有若干合法
    for p in batch:
        assert is_feasible(p) is True
        assert p["min_rr"] == 2.0              # normalize 过
        # trailing 一致性：grace=0 时 step/floor 必为 0
        if p["trailing_grace"] == 0:
            assert p["trailing_step"] == 0.0
            assert p["trailing_floor"] == 0.0


def test_sample_search_has_21_dims():
    """每条采样覆盖 21 维键。"""
    from discovery.sampler import sample_search
    from discovery.constraints import PARAM_KEYS
    batch = sample_search(n_sobol=5, n_random=5, seed=1)
    for p in batch:
        for k in PARAM_KEYS:
            assert k in p, f"缺参数 {k}"


def test_sample_search_reproducible():
    """同 seed 同 n → 同 batch（可复现）。"""
    from discovery.sampler import sample_search
    a = sample_search(n_sobol=10, n_random=5, seed=99)
    b = sample_search(n_sobol=10, n_random=5, seed=99)
    assert len(a) == len(b)
    # 每条 dict 值相等（None 也要相等）
    for x, y in zip(a, b):
        assert x == y
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_sampler.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.sampler'`）。

- [ ] **Step 3: 实现 `discovery/sampler.py`**

```python
# -*- coding: utf-8 -*-
"""L3 采样层（spec §7.2，自写 Sobol 准随机 + random 补充，零新增依赖）。

物理意图（spec §7.2 / §3.5 判据④）：纯随机/贪心在 21 维空间覆盖极不均匀（聚集+盲区
并存），是伪收敛的元凶。Sobol 准随机序列（低差异序列）以远低于纯随机的样本数达到空间
均匀覆盖——这是收敛判据④（覆盖度）达标的物理手段，"先铺满再谈优化"。

Plan 2 范围：Sobol 初始覆盖 + random 补充（满足 sample_search 接口）。**TPE 序贯优化留
Plan 3**（需 OOS 目标函数 + 后验拟合，Plan 2 仅立采样骨架）。

反魔法决策（ADR4）：spec §7.2 写 "optuna.samplers.SobolSampler 或自写"。实证 optuna 未
安装（.venv310 import 失败）。本模块**自写 Sobol**（Joe & Kuo 2008 标准方向数，纯 Python），
零新增依赖，符合 Karpathy 极简——Sobol 算法本身是公开数学，不必引重型库。
"""
import numpy as np

# 复用 scripts/param_iter.PARAM_SPACE（21 维候选档，同源不重造）
from discovery.tools.param_iter import PARAM_SPACE as _PARAM_SPACE_RAW

# 整理为有序 [(key, [candidates])]（去掉 layer 标记，顺序与 PARAM_KEYS 一致）
PARAM_SPACE = [(k, cands) for k, _layer, cands in _PARAM_SPACE_RAW]
_PARAM_KEYS = [k for k, _ in PARAM_SPACE]
_CANDIDATES = [cands for _, cands in PARAM_SPACE]

# Sobol 方向数（Joe & Kuo 2008，前 21 维的标准 primitive polynomial + direction numbers）。
# dim 索引 1..21 对应标准 Sobol 序列的第 1..21 维。数值取自 Joe & Kuo 官方表
# （https://web.maths.unsw.edu.au/~fkuo/sobol/），每维用位运算生成。
# 这里用查表形式存前 21 维的 direction numbers（s, a, m 序列），避免运行时算多项式。
# 格式：_SOBOL_DN[dim_index] = (s_k, a_k, [m_1..m_s])
# 为保持模块紧凑，用预计算的整数 direction vector（每维 32-bit）。
# 来源：Joe & Kuo (2008) "Constructing Sobol sequences with better two-dimensional projections"
_SOBOL_DIRECTION_NUMBERS = {
    # dim: (degree, a_coeff, [m_1, m_2, ...])  —— 仅列前 21 维（够 PARAM_KEYS=21）
    1: (1, 0, [1]),
    2: (2, 1, [1, 3]),
    3: (3, 1, [1, 3, 1]),
    4: (3, 2, [1, 1, 1]),
    5: (4, 1, [1, 1, 3, 1]),
    6: (4, 4, [1, 3, 5, 7]),
    7: (5, 2, [1, 7, 11, 13, 1]),
    8: (5, 6, [1, 1, 5, 3, 7]),
    9: (5, 5, [1, 3, 1, 7, 5]),
    10: (5, 0, [1, 1, 1, 1, 1]),
    11: (5, 3, [1, 3, 5, 7, 13]),
    12: (5, 7, [1, 1, 5, 11, 7]),
    13: (6, 4, [1, 1, 1, 3, 9, 17]),
    14: (6, 6, [1, 1, 3, 5, 1, 11]),
    15: (6, 1, [1, 3, 1, 1, 9, 5]),
    16: (6, 3, [1, 1, 5, 5, 9, 7]),
    17: (6, 5, [1, 3, 7, 7, 1, 17]),
    18: (6, 7, [1, 1, 1, 15, 13, 23]),
    19: (6, 2, [1, 1, 7, 11, 19, 3]),
    20: (6, 4, [1, 1, 3, 7, 19, 9]),
    21: (6, 6, [1, 3, 5, 13, 1, 11]),
}

# 预计算 32-bit direction vectors（V[i] for i=1..dim，每个是 32 元素 np.uint32 数组）
def _compute_direction_vectors(max_dim=21, n_bits=32):
    """从 direction numbers 生成 32-bit V[i][j] 数组（Joe & Kuo 算法）。

    V[d][j] = 第 d 维第 j 位的方向数（j=0..31）。生成第 k 个 Sobol 点用 XOR of V[*][j]
    where j-th bit of k is set。
    """
    V = np.zeros((max_dim + 1, n_bits), dtype=np.uint32)   # 1-indexed dim
    for d in range(1, max_dim + 1):
        s, a, m = _SOBOL_DIRECTION_NUMBERS[d]
        # 初始化 V[d][0..s-1] from m
        for j in range(s):
            V[d][j] = m[j] << (n_bits - 1 - j)
        # 递推 V[d][s..n_bits-1]
        if a > 0:
            for j in range(s, n_bits):
                v = V[d][j - s] ^ (V[d][j - s] >> s)
                for k in range(s - 1):
                    if (a >> (s - 1 - k)) & 1:
                        v ^= V[d][j - s + k + 1] >> (s - 1 - k)
                V[d][j] = v
        else:
            # a=0 时（如 dim 1/10）按 m 幂次填，其余位递推退化为简单移位
            for j in range(s, n_bits):
                V[d][j] = V[d][j - s] >> 1 if s >= 1 else V[d][j - s]
    return V


_DIRECTION_VECTORS = _compute_direction_vectors(21, 32)


def sobol_sample(dim, n, seed=0):
    """自写 Sobol 准随机序列（Joe & Kuo 2008），shape=(n, dim)，值∈[0,1)。

    算法：第 k 个点的第 d 维 = XOR of V[d][j] for all j where bit-j of k is set，
    再除以 2^32 归一到 [0,1)。dim ≤ 21（覆盖 PARAM_KEYS 21 维）。
    seed 通过跳过前 seed×n 个点实现确定性偏移（同 seed 同 dim 同 n → 同输出）。
    """
    assert dim <= 21, f"Sobol 仅支持 dim≤21（PARAM_KEYS=21），got {dim}"
    V = _DIRECTION_VECTORS   # (22, 32) uint32
    # 起点 index = seed * n（确定性偏移，让不同 seed 出不同序列）
    start = int(seed) * n
    out = np.zeros((n, dim), dtype=np.float64)
    # 累积 XOR：用 Gray code (G(k) = k XOR (k>>1)) 让相邻点只翻一位
    g = start ^ (start >> 1)   # 初始 Gray code
    cur = np.zeros(dim, dtype=np.uint32)
    # 初始化 cur = point[start]
    if start > 0:
        for j in range(32):
            if (g >> j) & 1:
                for d in range(dim):
                    cur[d] ^= V[d + 1][j]
    for i in range(n):
        # 输出当前点
        for d in range(dim):
            out[i, d] = cur[d] / 4294967296.0   # /2^32
        # 推进到下一个点：用 Gray code，翻 j = ctz(i+1+start) 位
        idx = start + i + 1
        j = 0
        tmp = idx
        while (tmp & 1) == 0 and tmp > 0:
            j += 1
            tmp >>= 1
        if j < 32:
            for d in range(dim):
                cur[d] ^= V[d + 1][j]
    return out


def random_sample(dim, n, seed=0):
    """纯随机采样（np.random.default_rng），shape=(n, dim)，值∈[0,1)。

    作 Sobol 覆盖的补充（spec §7.2：Sobol 初始覆盖 + random 补充，TPE 留 Plan 3）。
    """
    rng = np.random.default_rng(seed)
    return rng.random((n, dim))


def _scale_to_candidates(unit_vec, candidates_per_dim):
    """单位向量 [0,1)^dim → 每维候选档索引 → 取候选档值 → params dict。

    unit_vec[d]∈[0,1) → idx = int(unit_vec[d] × len(cands))，clamp 到 [0, len-1]。
    候选档可为任意类型（int/float/None），直接索引取值。
    """
    p = {}
    for d, cands in enumerate(candidates_per_dim):
        u = unit_vec[d]
        idx = int(u * len(cands))
        idx = max(0, min(len(cands) - 1, idx))
        p[_PARAM_KEYS[d]] = cands[idx]
    return p


def _unit_vecs_to_params(unit_mat):
    """(n, dim) 单位矩阵 → list[dict]（每维映射到候选档）。"""
    return [_scale_to_candidates(unit_mat[i], _CANDIDATES) for i in range(unit_mat.shape[0])]


def sample_search(n_sobol, n_random, seed=0, n_attempts_factor=3):
    """约束裁剪后的合法采样流：Sobol 初始覆盖 + random 补充。

    流程：采样 (n_sobol + n_random) × factor 候选 → filter_feasible 裁剪 → 取前
    (n_sobol + n_random) 合法（factor 兜底废组合密度）。不够则继续 random 补采直至达量
    或 attempts 上限（防约束过紧死循环）。
    返回 list[dict]（已 normalize，21 维齐全，约束合法）。
    """
    from discovery.constraints import filter_feasible
    target = n_sobol + n_random
    if target == 0:
        return []
    # 阶段一：Sobol 初始覆盖（spec §7.2 "先铺满"）
    dim = len(_PARAM_KEYS)
    sob_unit = sobol_sample(dim, n_sobol, seed=seed)
    sob_params = _unit_vecs_to_params(sob_unit)
    # 阶段二：random 补充
    rnd_unit = random_sample(dim, n_random, seed=seed + 1)
    rnd_params = _unit_vecs_to_params(rnd_unit)
    # 合并 + 裁剪
    batch = filter_feasible(sob_params + rnd_params)
    # 不够则 random 继续补采（attempts 上限防死循环）
    attempts = 0
    extra_seed = seed + 100
    while len(batch) < target and attempts < n_attempts_factor * 5:
        more = random_sample(dim, target * 2, seed=extra_seed)
        batch.extend(filter_feasible(_unit_vecs_to_params(more)))
        extra_seed += 1
        attempts += 1
    return batch[:target]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_sampler.py -v
```
Expected: 9 passed。若 `test_sobol_uniformity_better_than_random` 偶发失败（n=64 样本量小，方差有随机性），把 n 提到 128 或放宽 `<= rnd.std() + 2`（Sobol 应稳定优于随机，但小样本波动留 1-2 容差）。

- [ ] **Step 5: Commit**

```bash
git add discovery/sampler.py tests/discovery/test_sampler.py
git commit -m "feat(discovery): L3 采样层 自写 Sobol+random（零新增依赖，spec §7.2）

- sobol_sample：Joe & Kuo 2008 标准方向数，dim≤21，确定性可复现
- sobol 一维投影桶计数方差 ≤ 纯随机（低差异覆盖，判据④物理手段）
- _scale_to_candidates：单位向量→候选档（复用 param_iter.PARAM_SPACE 同源）
- sample_search：Sobol 初始覆盖+random 补充，filter_feasible 裁剪后产合法流
- 反魔法：optuna 未装，自写 Sobol 纯 Python（spec §7.2 允许"或自写"）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: ProcessPool Worker（`discovery/worker.py`，Windows spawn 兼容）

**Files:**
- Create: `discovery/worker.py`、`tests/discovery/test_worker.py`

**Interfaces:**
- Consumes: `discovery/snapshot.freeze`（Plan 1，initializer 一次加载 universe 复用）；`discovery/objective.evaluate`（Plan 1，单组评估）；`discovery/split.holdout_split`（Plan 1）；`multiprocessing.Pool`（标准库）。
- Produces: 顶层 `_init_worker(lake_start, embargo_days)`（Pool initializer，子进程加载 snapshot universe + split 到模块全局，复用避免每 trial 重读 parquet）；顶层 `_eval_worker(params) -> (params, result_dict)`（Pool.map 调用，**可 pickle**，调 `objective.evaluate`，异常返回 `None` 单 trial 失败不影响 run）；`_WORKER_STATE`（模块级单例，子进程内持有 universe/split）；`eval_batch(params_list, lake_start, embargo_days, n_proc=None) -> list`（便捷封装：建 Pool + map + 收集，主进程用）。

**Windows spawn 兼容关键**（spec §8 拷问② + Global Constraints）：
1. `_init_worker`/`_eval_worker` 必须**顶层定义**（不能嵌套/lambda/闭包）——spawn 用 pickle 序列化函数引用，嵌套函数不可 pickle。
2. 子进程会重新 import `discovery.worker` 模块——故模块顶层不能有副作用（不能在 import 时 freeze）。
3. `python -m discovery` 入口必须 `if __name__ == "__main__":` 守护（Task 5 cli 已有，spawn 重新 import 时不重复执行 main）。
4. 单 trial 异常 → `_eval_worker` 捕获返回 `None`，主进程过滤 null（spec §8：单 trial 失败不影响 run）。
5. universe 通过 initializer 注入子进程全局（**不随每 params pickle**，否则 455MB parquet 每次序列化爆掉）。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_worker.py`:
```python
# -*- coding: utf-8 -*-
"""ProcessPool worker 测试：顶层可 pickle + initializer 复用 + 单组评估返回结构。

合成 universe 快测（不真实 freeze，monkeypatch freeze 注入合成数据，避免读 455MB parquet）；
Pool 真实起子进程标 slow。
"""
import pickle
import pytest


def test_init_worker_is_toplevel_picklable():
    """_init_worker 顶层定义 → 可 pickle（Windows spawn 必需，spec §8 拷问②）。"""
    from discovery import worker
    # 模块级函数引用可 pickle（pickle by qualified name）
    blob = pickle.dumps(worker._init_worker)
    assert pickle.loads(blob) is worker._init_worker


def test_eval_worker_is_toplevel_picklable():
    """_eval_worker 顶层定义 → 可 pickle。"""
    from discovery import worker
    blob = pickle.dumps(worker._eval_worker)
    assert pickle.loads(blob) is worker._eval_worker


def test_init_worker_not_lambda_not_nested():
    """_init_worker 不是 lambda/闭包（spawn 不可 pickle 的反例）。"""
    from discovery import worker
    fn = worker._init_worker
    assert hasattr(fn, "__qualname__")
    assert "<lambda>" not in fn.__qualname__
    assert "<locals>" not in fn.__qualname__   # 嵌套函数会有 <locals>


def test_eval_worker_uses_state(monkeypatch, champion_params, synth_sym_df):
    """_eval_worker 在 _init_worker 设好 state 后调，返回 (params, evaluate 结果)。

    monkeypatch freeze 注入合成 universe（避免读 parquet）；_init_worker 设 _WORKER_STATE；
    _eval_worker 读 state 调 evaluate。单进程模拟（不起 Pool，验证逻辑链）。
    """
    from discovery import worker
    from discovery.split import HoldoutSplit, Segment
    from datetime import date

    # monkeypatch freeze 返回合成 universe + 假 meta
    class FakeMeta:
        snapshot_hash = "fakehash"
        universe_count = 1
    monkeypatch.setattr(worker, "freeze", lambda start="2025-01-01": ({"300001.SZ": synth_sym_df}, FakeMeta()))
    monkeypatch.setattr(worker, "holdout_split", lambda embargo_days=5: HoldoutSplit(
        Segment("i", date(2025, 1, 1), date(2025, 12, 31)),
        Segment("o", date(2026, 1, 1), date(2026, 12, 31)),
        embargo_days,
    ))

    worker._init_worker("2025-01-01", 5)
    out = worker._eval_worker(champion_params)
    assert out is not None
    params_back, res = out
    assert params_back == champion_params
    assert set(res.keys()) >= {"inner", "outer", "n_total"}


def test_eval_worker_swallows_exception(monkeypatch, champion_params, synth_sym_df):
    """_eval_worker 单组异常 → 返回 None（spec §8 单 trial 失败不影响 run）。"""
    from discovery import worker
    from discovery.split import HoldoutSplit, Segment
    from datetime import date

    class FakeMeta:
        snapshot_hash = "fakehash"
        universe_count = 1
    monkeypatch.setattr(worker, "freeze", lambda start="2025-01-01": ({"300001.SZ": synth_sym_df}, FakeMeta()))
    monkeypatch.setattr(worker, "holdout_split", lambda embargo_days=5: HoldoutSplit(
        Segment("i", date(2025, 1, 1), date(2025, 12, 31)),
        Segment("o", date(2026, 1, 1), date(2026, 12, 31)), embargo_days,
    ))
    # monkeypatch evaluate 抛异常
    monkeypatch.setattr(worker, "evaluate", lambda p, u, s: (_ for _ in ()).throw(RuntimeError("boom")))

    worker._init_worker("2025-01-01", 5)
    out = worker._eval_worker(champion_params)
    assert out is None


@pytest.mark.slow
def test_eval_batch_real_pool(champion_params):
    """集成：真实 Pool 起 2 子进程跑 2 组（含冠军 + 邻域扰动），~6min。
    验证 ProcessPool 真起作用、子进程 freeze 复用、返回非 None。"""
    from discovery.worker import eval_batch
    # 冠军 + 轻微扰动（window 80→60）作第二组
    p2 = dict(champion_params); p2["window"] = 60
    results = eval_batch([champion_params, p2], lake_start="2025-01-01",
                         embargo_days=5, n_proc=2)
    assert len(results) == 2
    # 至少冠军组应非 None（真实数据有信号）
    non_none = [r for r in results if r is not None]
    assert len(non_none) >= 1
    for params_back, res in non_none:
        assert "inner" in res and "outer" in res
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_worker.py -v -m "not slow"
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.worker'`）。

- [ ] **Step 3: 实现 `discovery/worker.py`**

```python
# -*- coding: utf-8 -*-
"""L2 ProcessPool worker（spec §4.3 / §8 拷问②，Windows spawn 兼容）。

物理意图：Plan 1 单组 evaluate ~3min，串行跑 N 组要 N×3min。ProcessPool 并发把 N 组
分到 (核数-2) 子进程，吞吐 ×(核数-2)（spec §3.6 算力账）。每子进程一次 freeze 加载
universe（~5s，455MB parquet），后续多组复用——避免每 trial 重读 parquet（ADR6 复用
backtest/worker.py 的 initializer 模式，但本模块自包含不依赖未确认的 backtest/worker.py）。

Windows spawn 兼容（spec §8 拷问② + Global Constraints）：
1. _init_worker / _eval_worker 必须**顶层定义**——spawn 用 pickle 序列化函数引用，嵌套/
   lambda/闭包不可 pickle 会 raise AttributeError。
2. 子进程会重新 import 本模块——顶层不能有副作用（不能 import 时 freeze）。
3. universe 通过 initializer 注入子进程模块全局 _WORKER_STATE，**不随每 params pickle**
   （否则 455MB parquet 每次 map 都序列化，爆掉）。
4. _eval_worker 捕获单组异常返回 None——主进程 filter null（spec §8 单 trial 失败不影响 run）。
"""
import multiprocessing as mp
import os

# 顶层 import（子进程重新 import 时执行，无副作用）
from discovery.snapshot import freeze
from discovery.split import holdout_split
from discovery.objective import evaluate

# 子进程模块全局：initializer 一次 freeze 后填充，_eval_worker 读取复用
_WORKER_STATE = {"universe": None, "split": None, "ready": False}


def _init_worker(lake_start="2025-01-01", embargo_days=5):
    """Pool initializer：子进程启动时一次 freeze 加载 universe + split 到 _WORKER_STATE。

    顶层定义（可 pickle）。lake_start/embargo_days 是简单类型（str/int），可 pickle 跨进程传。
    后续 _eval_worker 调用复用此 universe，不重读 parquet。
    """
    universe, _meta = freeze(lake_start=lake_start)
    split = holdout_split(embargo_days=embargo_days)
    _WORKER_STATE["universe"] = universe
    _WORKER_STATE["split"] = split
    _WORKER_STATE["ready"] = True


def _eval_worker(params):
    """Pool.map 调用：评估单组 params，返回 (params, result_dict) 或 None（异常）。

    顶层定义（可 pickle）。读 _WORKER_STATE（initializer 设的 universe/split），调
    objective.evaluate。异常捕获返回 None——主进程 filter null，单 trial 失败不影响 run
    （spec §8 拷问②：worker 崩溃 → 单 trial 标 failed，run 继续）。
    """
    if not _WORKER_STATE["ready"]:
        return None
    try:
        res = evaluate(params, _WORKER_STATE["universe"], _WORKER_STATE["split"])
        return (params, res)
    except Exception:
        return None


def _default_n_proc():
    """默认进程数 = 核数 - 2（留 2 核给系统/主进程，spec §3.6 夜跑吞吐估算用）。"""
    return max(1, (os.cpu_count() or 4) - 2)


def eval_batch(params_list, lake_start="2025-01-01", embargo_days=5, n_proc=None):
    """便捷封装：主进程建 Pool + initializer + map _eval_worker + 收集结果。

    返回 list，每项为 (params, result_dict) 或 None（异常组）。调用方按需 filter null。
    n_proc=None → 默认核数-2。空输入返回 []。
    """
    if not params_list:
        return []
    if n_proc is None:
        n_proc = _default_n_proc()
    n_proc = min(n_proc, len(params_list))   # 不超过任务数
    # Windows spawn：用 'spawn' 显式（默认即 spawn，显式更清晰可移植）
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=n_proc, initializer=_init_worker,
                  initargs=(lake_start, embargo_days)) as pool:
        results = pool.map(_eval_worker, params_list)
    return results
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_worker.py -v -m "not slow"
```
Expected: 5 passed（pickle/顶层/init/eval/异常吞）。slow 集成（真实 Pool）跳过。

- [ ] **Step 5: Commit**

```bash
git add discovery/worker.py tests/discovery/test_worker.py
git commit -m "feat(discovery): L2 ProcessPool worker Windows spawn 兼容（spec §8 拷问②）

- _init_worker/_eval_worker 顶层定义可 pickle（spawn 必需，禁 lambda/闭包/嵌套）
- initializer 一次 freeze 加载 universe 复用（避免每 trial 重读 455MB parquet）
- _eval_worker 异常返回 None（单 trial 失败不影响 run，spec §8）
- eval_batch 便捷封装：Pool+initializer+map，n_proc=核数-2
- 用标准 multiprocessing.Pool（反幻觉：不用未确认的 backtest/worker.py）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: 跑批调度（`discovery/runner.py`，断点续跑去重）

**Files:**
- Create: `discovery/runner.py`、`tests/discovery/test_runner.py`

**Interfaces:**
- Consumes: `discovery/sampler.sample_search`（Task 2）；`discovery/worker.eval_batch`（Task 3）；`discovery/store.{init_db,connect,write_trial,write_snapshot,trial_id_of,trial_exists}`（Plan 1）；`discovery/snapshot.freeze/SnapshotMeta`（Plan 1）；`discovery/split.holdout_split`（Plan 1）；`discovery/cli._engine_hash`（Plan 1，复用内核指纹）。
- Produces: `run_search(snapshot_meta, split, budget, n_sobol, n_random, seed, db_path, n_proc=None, lake_start="2025-01-01") -> dict`（跑批主函数）；`_persist_trial(conn, params, snapshot_meta, split_tag, engine_hash, result, source, seed) -> str|None`（落库单 trial，返回 trial_id 或 None 若已存在/失败）；`RunSummary` dataclass（含 `n_sampled`/`n_evaluated`/`n_new_trials`/`n_skipped_dup`/`n_failed`/`top_inner_calmar`/`db_path`）。

**关键设计**：
- **断点续跑去重**：采样后先用 `trial_id_of` + `trial_exists` 过滤已落库的（kill/重启场景），只把未跑的送 Pool。`write_trial` 用 `INSERT OR IGNORE`（Plan 1）二次保险。
- **信息隔离**：落库写 inner + outer metrics（完整记录），但**汇总/排序只用 inner**（spec §6.2：搜索不反馈 outer；outer 留报告）。`top_inner_calmar` 取 `feasibility_gate` 过滤后 calmar 最高（复用 Plan 1 judging）。
- **收敛判据占位**：Plan 2 跑完 budget 即停，返回 RunSummary（Pareto 前沿 + 连续K轮判据 + 覆盖度判据④留 Plan 3）。`status="budget_exhausted"`（Plan 3 扩 `"converged"`）。
- **budget 语义**：`budget` = 搜索组数上限（采样目标 n_sobol+n_random，受 budget 截断）。Plan 2 不做时间 budget（留 Plan 4 schtasks 守护）；`--budget N` 指"最多跑 N 组新 trial"。

- [ ] **Step 1: 写失败测试**

`tests/discovery/test_runner.py`:
```python
# -*- coding: utf-8 -*-
"""跑批调度测试：断点续跑去重 + 采样落库计数 + 汇总字段。

合成 universe 快测（monkeypatch freeze/eval_batch 避免真实 Pool+parquet）；
真实跑批集成在 Task 5 cli slow 测试。
"""
from datetime import date


def _fake_meta():
    from discovery.snapshot import SnapshotMeta
    return SnapshotMeta("snaptest", "创板科创", 1, "2025~2026", "2025-01-01")


def _fake_split():
    from discovery.split import HoldoutSplit, Segment
    return HoldoutSplit(Segment("i", date(2025, 1, 1), date(2025, 12, 31)),
                        Segment("o", date(2026, 1, 1), date(2026, 12, 31)), 5)


def test_persist_trial_writes_and_returns_id(tmp_path, monkeypatch):
    """_persist_trial 落库返回 trial_id（新 trial）。"""
    from discovery import runner
    from discovery.store import init_db, connect, trial_exists
    db = str(tmp_path / "t.db")
    init_db(db)
    params = {"window": 80, "min_rr": 2.0}
    result = {"inner": {"ann": 0.5, "calmar": 2.0, "max_dd": 0.2, "n": 100},
              "outer": {"ann": 1.5, "calmar": 5.0, "max_dd": 0.3, "n": 80}, "n_total": 180}
    with connect(db) as conn:
        tid = runner._persist_trial(conn, params, _fake_meta(), "holdout_2025_2026",
                                    "eng1", result, "sobol", seed=0)
    assert tid is not None
    with connect(db) as conn:
        assert trial_exists(conn, tid) is True


def test_persist_trial_returns_none_on_dup(tmp_path):
    """同 params+snapshot+seed 已存在 → 返回 None（断点续跑去重）。"""
    from discovery import runner
    from discovery.store import init_db, connect
    db = str(tmp_path / "t.db")
    init_db(db)
    params = {"window": 80}
    result = {"inner": {"ann": 0.5}, "outer": {"ann": 1.5}, "n_total": 10}
    with connect(db) as conn:
        tid1 = runner._persist_trial(conn, params, _fake_meta(), "s", "e", result, "sobol", 0)
        tid2 = runner._persist_trial(conn, params, _fake_meta(), "s", "e", result, "sobol", 0)
    assert tid1 is not None
    assert tid2 is None   # 重复落库被 INSERT OR IGNORE 吞


def test_run_search_dedup_on_restart(tmp_path, monkeypatch):
    """kill 重启场景：已落 trial 不重跑（eval_batch 不再被调用于已存组）。"""
    from discovery import runner
    from discovery.store import init_db, connect
    db = str(tmp_path / "t.db")
    init_db(db)

    # monkeypatch sample_search 产固定 3 组（可复现 seed）
    fixed_params = [
        {"window": 40, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0},
        {"window": 60, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0},
        {"window": 80, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0},
    ]
    monkeypatch.setattr(runner, "sample_search", lambda **kw: fixed_params)
    # monkeypatch eval_batch 直接造结果（不真实起 Pool）
    def fake_eval(plist, **kw):
        return [(p, {"inner": {"ann": 0.5, "calmar": 2.0, "max_dd": 0.2, "n": 100},
                     "outer": {"ann": 1.5, "calmar": 5.0, "max_dd": 0.3, "n": 80},
                     "n_total": 180}) for p in plist]
    monkeypatch.setattr(runner, "eval_batch", fake_eval)
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")

    # 第一次跑：3 组全落库
    s1 = runner.run_search(_fake_meta(), _fake_split(), budget=3, n_sobol=2, n_random=1,
                           seed=42, db_path=db)
    assert s1.n_new_trials == 3
    assert s1.n_skipped_dup == 0
    # 第二次跑（模拟重启，同 seed 同采样）：3 组已存，全跳过
    s2 = runner.run_search(_fake_meta(), _fake_split(), budget=3, n_sobol=2, n_random=1,
                           seed=42, db_path=db)
    assert s2.n_new_trials == 0
    assert s2.n_skipped_dup == 3


def test_run_search_summary_fields(tmp_path, monkeypatch):
    """RunSummary 字段齐全（n_sampled/n_evaluated/n_new/n_skipped/n_failed/top_inner_calmar）。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    monkeypatch.setattr(runner, "sample_search", lambda **kw: [
        {"window": 80, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0}])
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [
        (plist[0], {"inner": {"ann": 0.5, "calmar": 3.5, "max_dd": 0.2, "n": 100},
                    "outer": {"ann": 1.5, "calmar": 5.0, "max_dd": 0.3, "n": 80}, "n_total": 180})])
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)
    assert s.n_sampled == 1
    assert s.n_evaluated == 1
    assert s.n_new_trials == 1
    assert s.n_failed == 0
    assert s.top_inner_calmar == 3.5
    assert s.db_path == db
    assert s.status == "budget_exhausted"


def test_run_search_failed_trial_filtered(tmp_path, monkeypatch):
    """eval_batch 返回 None（异常组）→ n_failed 计数，不落库。"""
    from discovery import runner
    from discovery.store import init_db
    db = str(tmp_path / "t.db")
    init_db(db)
    monkeypatch.setattr(runner, "sample_search", lambda **kw: [
        {"window": 80, "min_rr": 2.0, "tp1_h_mult": 1.0, "tp_h_mult": 2.5,
         "cancel_thresh_mult": 3.0, "trailing_grace": 5, "trailing_step": 0.1, "trailing_floor": 0.0}])
    monkeypatch.setattr(runner, "eval_batch", lambda plist, **kw: [None])  # 全失败
    monkeypatch.setattr(runner, "_engine_hash", lambda: "eng1")
    s = runner.run_search(_fake_meta(), _fake_split(), budget=1, n_sobol=1, n_random=0,
                          seed=1, db_path=db)
    assert s.n_failed == 1
    assert s.n_new_trials == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_runner.py -v
```
Expected: FAIL（`ModuleNotFoundError: No module named 'discovery.runner'`）。

- [ ] **Step 3: 实现 `discovery/runner.py`**

```python
# -*- coding: utf-8 -*-
"""L2 跑批调度（spec §4.3 / §5.1 / §8 拷问②，断点续跑去重）。

物理意图：把 Plan 1 的单组 evaluate 升级为"采样→并发评估→落库→去重续跑"的跑批循环。
Plan 2 范围：budget 驱动跑 N 组新 trial，落 SQLite，返回 RunSummary。**收敛判据（Pareto/
连续K轮/覆盖度④）占位**——status 恒 "budget_exhausted"，Plan 3 才扩 "converged"。

断点续跑去重（spec §8 拷问②）：采样后先 trial_id_of + trial_exists 过滤已落库组，只把
未跑的送 Pool。write_trial 用 INSERT OR IGNORE（Plan 1）二次保险。kill/重启自动接续。

信息隔离（spec §6.2）：落库写 inner+outer（完整记录），但 RunSummary.top_inner_calmar
只用 inner（feasibility_gate 过滤后 calmar 最高）——搜索不反馈 outer，outer 留报告。
"""
from dataclasses import dataclass, field
from typing import Any

from discovery.sampler import sample_search
from discovery.worker import eval_batch
from discovery.store import (init_db, connect, write_trial, write_snapshot,
                             trial_id_of, trial_exists)
from discovery.snapshot import SnapshotMeta
from discovery.split import HoldoutSplit
from discovery.judging import feasibility_gate, calmar_rank


def _engine_hash():
    """回测内核指纹（复用 cli._engine_hash 模式，避免循环 import：本地重声明）。

    Plan 1 cli._engine_hash 已实现（backtest.py+method_v0.py sha256[:12]）；本模块独立
    重声明同款逻辑，避免 discovery.cli → discovery.runner → discovery.cli 循环 import。
    """
    import hashlib
    from strategies.neckline import backtest, method_v0
    h = hashlib.sha256()
    for f in (backtest.__file__, method_v0.__file__):
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


@dataclass
class RunSummary:
    """跑批汇总（Plan 2：无收敛判据，status 恒 budget_exhausted；Plan 3 扩 converged）。"""
    n_sampled: int = 0          # 采样总数（filter_feasible 后）
    n_evaluated: int = 0        # 实际送 Pool 评估数（采样 - 已存去重）
    n_new_trials: int = 0       # 新落库 trial 数
    n_skipped_dup: int = 0      # 跳过的已存 trial（断点续跑去重）
    n_failed: int = 0           # 评估失败组数（eval_batch 返回 None）
    top_inner_calmar: float = 0.0   # 可行域内最高 inner calmar（信息隔离：不用 outer）
    top_trial_id: str = ""      # top_inner_calmar 对应的 trial_id
    db_path: str = ""
    status: str = "budget_exhausted"   # Plan 3 扩 "converged"
    snapshot_hash: str = ""


def _persist_trial(conn, params, snapshot_meta, split_tag, engine_hash, result, source, seed):
    """落库单 trial。返回 trial_id（新落）或 None（已存在 INSERT OR IGNORE 吞）。

    信息隔离：inner/outer 都落（完整记录供报告），但调用方排序只用 inner（spec §6.2）。
    """
    tid = trial_id_of(params, snapshot_meta.snapshot_hash, seed)
    if trial_exists(conn, tid):
        return None
    write_trial(conn, tid, params, snapshot_meta.snapshot_hash, engine_hash,
                split_tag, result["inner"], result["outer"], source)
    return tid


def _split_tag(split):
    """从 HoldoutSplit 生成落库 split 标签（如 'holdout_2025_2026'）。"""
    inner_y = split.inner.start.year
    outer_y = split.outer.start.year
    return f"holdout_{inner_y}_{outer_y}"


def run_search(snapshot_meta, split, budget, n_sobol, n_random, seed,
               db_path, n_proc=None, lake_start="2025-01-01"):
    """跑批主函数：采样→去重→并发评估→落库→汇总。

    流程（spec §5.1）：
    1. sample_search 产 n_sobol+n_random 合法 params（约束裁剪后）
    2. budget 截断（最多跑 budget 组新 trial 的目标采样量）
    3. trial_id_of+trial_exists 过滤已落库组（断点续跑去重）
    4. eval_batch 并发评估未跑组（Pool, initializer 复用 universe）
    5. _persist_trial 落库（INSERT OR IGNORE 二次保险）
    6. RunSummary 汇总（top_inner_calmar 用 inner，信息隔离）

    snapshot_meta: SnapshotMeta（freeze 已算好，避免每 run 重 freeze）。
    split: HoldoutSplit。budget: 采样目标上限（n_sobol+n_random 受其截断）。
    返回 RunSummary。
    """
    init_db(db_path)
    engine_hash = _engine_hash()
    split_tag = _split_tag(split)

    # 1. 采样（约束裁剪后合法流）
    n_sobol_eff = min(n_sobol, budget)
    n_random_eff = min(n_random, max(0, budget - n_sobol_eff))
    sampled = sample_search(n_sobol=n_sobol_eff, n_random=n_random_eff, seed=seed)
    n_sampled = len(sampled)

    # 2. 落 snapshot（每 run 刷新，upsert）
    with connect(db_path) as conn:
        write_snapshot(conn, snapshot_meta)

    # 3. 去重：过滤已落库组（断点续跑）
    to_eval = []
    n_skipped = 0
    with connect(db_path) as conn:
        for p in sampled:
            tid = trial_id_of(p, snapshot_meta.snapshot_hash, seed)
            if trial_exists(conn, tid):
                n_skipped += 1
            else:
                to_eval.append(p)

    # 4. 并发评估（空则跳过，避免 Pool 空转）
    results = []
    if to_eval:
        results = eval_batch(to_eval, lake_start=lake_start,
                             embargo_days=split.embargo_days, n_proc=n_proc)

    # 5. 落库 + 汇总
    n_new = 0
    n_failed = 0
    candidates = []   # (calmar, tid) for top
    with connect(db_path) as conn:
        for item in results:
            if item is None:
                n_failed += 1
                continue
            params, res = item
            tid = _persist_trial(conn, params, snapshot_meta, split_tag,
                                 engine_hash, res, _source_of(seed), seed)
            if tid is None:
                continue   # 竞态下被 IGNORE 吞（罕见）
            n_new += 1
            inner = res["inner"]
            if feasibility_gate(inner):
                candidates.append((inner["calmar"], tid))

    # top_inner_calmar（信息隔离：只用 inner）
    top_calmar = 0.0
    top_tid = ""
    if candidates:
        candidates.sort(reverse=True)
        top_calmar, top_tid = candidates[0]

    return RunSummary(
        n_sampled=n_sampled,
        n_evaluated=len(to_eval),
        n_new_trials=n_new,
        n_skipped_dup=n_skipped,
        n_failed=n_failed,
        top_inner_calmar=top_calmar,
        top_trial_id=top_tid,
        db_path=db_path,
        snapshot_hash=snapshot_meta.snapshot_hash,
    )


def _source_of(seed):
    """seed → source 标签（Plan 2 全用 sobol+random，source 统一 'discovery_search'）。"""
    return "discovery_search"
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_runner.py -v
```
Expected: 5 passed（persist/dedup/summary/failed/restart）。

- [ ] **Step 5: Commit**

```bash
git add discovery/runner.py tests/discovery/test_runner.py
git commit -m "feat(discovery): L2 跑批调度 采样→Pool→落库 断点续跑去重（spec §5.1/§8）

- run_search：sample_search→trial_id去重→eval_batch并发→_persist_trial落库→RunSummary
- 断点续跑：trial_id_of+trial_exists 过滤已存组（kill/重启自动接续）
- 信息隔离：落 inner+outer 完整记录，但 top_inner_calmar 只用 inner（spec §6.2）
- 收敛判据占位：status 恒 budget_exhausted（Pareto/覆盖度④留 Plan 3）
- _engine_hash 本地重声明（避免 cli↔runner 循环 import）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: CLI run 命令 + 集成（`discovery/cli.py` 修改 + 集成测试）

**Files:**
- Modify: `discovery/cli.py`（加 `cmd_run` + `run` 子解析器）；`discovery/__init__.py`（导出 Plan 2 API）
- Create: `tests/discovery/test_cli_run.py`（slow 集成）

**Interfaces:**
- Consumes: Task 1-4 全部（constraints/sampler/worker/runner）；Plan 1 cli 既有（`_db_path`/`_engine_hash`/`freeze`/`holdout_split`/`main`）。
- Produces: `python -m discovery run --budget N --n-sobol M --n-random K --embargo D [--n-proc P] [--seed S] [--lake-start DATE]`——冻结快照→采样→并发跑批→落库→打印 RunSummary。

- [ ] **Step 1: 写集成测试（slow，跑真实 data_lake 小 budget）**

`tests/discovery/test_cli_run.py`:
```python
# -*- coding: utf-8 -*-
"""cli run 集成测试：subprocess 跑 python -m discovery run（小 budget），验证落库 + 报告。"""
import os
import subprocess
import sys

import pytest


@pytest.mark.slow
def test_run_command_produces_trials(tmp_path):
    """跑 discovery run --budget 2 --n-sobol 1 --n-random 1（~6min，2 组×3min/2核），
    exit 0，stdout 含 RunSummary 关键字段，SQLite 有 trial 落库。用 tmp_path 隔离 db。"""
    db = tmp_path / "run.db"
    env = {**os.environ, "DISCOVERY_DB": str(db)}
    proc = subprocess.run(
        [sys.executable, "-m", "discovery", "run",
         "--budget", "2", "--n-sobol", "1", "--n-random", "1",
         "--embargo", "5", "--n-proc", "2", "--seed", "42"],
        capture_output=True, text=True, env=env, cwd=os.getcwd(),
    )
    assert proc.returncode == 0, proc.stderr
    # RunSummary 关键字段
    assert "RunSummary" in proc.stdout or "n_new_trials" in proc.stdout
    assert "snapshot:" in proc.stdout
    # SQLite 落库校验
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    n = conn.execute("SELECT COUNT(*) c FROM trial").fetchone()["c"]
    conn.close()
    assert n >= 1   # 至少 1 组落库（可能 1 组异常 None）


@pytest.mark.slow
def test_run_command_dedup_on_rerun(tmp_path):
    """同 seed 同 budget 重跑 → 第二次 n_skipped_dup == budget（断点续跑去重）。"""
    db = tmp_path / "run2.db"
    env = {**os.environ, "DISCOVERY_DB": str(db)}
    args = [sys.executable, "-m", "discovery", "run",
            "--budget", "1", "--n-sobol", "1", "--n-random", "0",
            "--embargo", "5", "--n-proc", "1", "--seed", "7"]
    # 第一次
    p1 = subprocess.run(args, capture_output=True, text=True, env=env, cwd=os.getcwd())
    assert p1.returncode == 0, p1.stderr
    # 第二次（同 seed 同 budget）
    p2 = subprocess.run(args, capture_output=True, text=True, env=env, cwd=os.getcwd())
    assert p2.returncode == 0, p2.stderr
    # 第二次应全跳过（断点续跑）—— n_skipped_dup >= 1
    assert "n_skipped_dup" in p2.stdout
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/test_cli_run.py -v -m "not slow"
```
Expected: FAIL（`run` 子命令不存在；subprocess 跑会报错 `invalid choice: 'run'`；slow 跳过）。

- [ ] **Step 3: 实现 `cmd_run` + 加 `run` 子解析器**

在 `discovery/cli.py` 的 `main()` **之前**加 `cmd_run`（紧跟 `cmd_verify` 之后），并在 `main()` 的子解析器注册区加 `run`:

`discovery/cli.py` 新增 `cmd_run`（在 `cmd_verify` 函数后插入）:
```python
def cmd_run(args):
    """并发搜索跑批：采样→Pool→落库（spec §5.1，Plan 2 L2+L3 基础）。"""
    from discovery.runner import run_search
    universe, meta = freeze(args.lake_start)
    split = holdout_split(args.embargo)
    print(f"=== discovery run：并发搜索（snapshot={meta.snapshot_hash}）===")
    print(f"snapshot: {meta.universe_count} 只 | {meta.date_range} | hash {meta.snapshot_hash}")
    print(f"配置: budget={args.budget} sobol={args.n_sobol} random={args.n_random} "
          f"proc={args.n_proc} seed={args.seed} embargo={args.embargo}")
    summary = run_search(meta, split, budget=args.budget, n_sobol=args.n_sobol,
                         n_random=args.n_random, seed=args.seed, db_path=_db_path(),
                         n_proc=args.n_proc, lake_start=args.lake_start)
    print(f"--- RunSummary ---")
    print(f"n_sampled={summary.n_sampled} n_evaluated={summary.n_evaluated} "
          f"n_new_trials={summary.n_new_trials} n_skipped_dup={summary.n_skipped_dup} "
          f"n_failed={summary.n_failed}")
    print(f"top_inner_calmar={summary.top_inner_calmar:.2f} "
          f"top_trial_id={summary.top_trial_id} status={summary.status}")
    print(f"db={summary.db_path}")
    print(f"信息隔离: 汇总只用 inner calmar（搜索不反馈 outer，spec §6.2）；"
          f"Plan 2 无收敛判据（Pareto/覆盖度④留 Plan 3）")
```

在 `main()` 的子解析器注册区（紧跟 `ap_v` 之后）加:
```python
    ap_r = sub.add_parser("run", help="并发搜索跑批（L2+L3 基础，Plan 2）")
    ap_r.add_argument("--budget", type=int, default=10, help="采样目标上限（最多跑 N 组新 trial）")
    ap_r.add_argument("--n-sobol", type=int, default=5, dest="n_sobol", help="Sobol 初始覆盖组数")
    ap_r.add_argument("--n-random", type=int, default=5, dest="n_random", help="random 补充组数")
    ap_r.add_argument("--embargo", type=int, default=5, help="inner→outer embargo 天数")
    ap_r.add_argument("--n-proc", type=int, default=None, dest="n_proc",
                     help="进程数（默认核数-2）")
    ap_r.add_argument("--seed", type=int, default=42, help="采样种子（可复现）")
    ap_r.add_argument("--lake-start", type=str, default="2025-01-01", dest="lake_start",
                     help="universe 加载起始日")
    ap_r.set_defaults(func=cmd_run)
```

`discovery/__init__.py` 更新导出（在现有导出后追加 Plan 2 API）:
```python
# -*- coding: utf-8 -*-
"""参数发现引擎（spec 2026-07-23-param-discovery-engine-design.md v1.3）。

Plan 1（L0+L1 可信度闭环）：快照冻结 + 2025/2026 holdout 嵌套 OOS + 分层裁判最小版。
Plan 2（L2 吞吐 + L3 搜索基础）：约束裁剪 + Sobol/random 采样 + ProcessPool 并发 + 断点续跑。
"""
from discovery.snapshot import freeze, SnapshotMeta, snapshot_hash
from discovery.split import holdout_split, Segment, HoldoutSplit
from discovery.objective import evaluate, run_full_scan, segment_metrics, metrics_of
from discovery.judging import feasibility_gate, calmar_rank
# Plan 2 新增
from discovery.constraints import normalize_params, is_feasible, filter_feasible, PARAM_KEYS
from discovery.sampler import sample_search, sobol_sample, random_sample, PARAM_SPACE
from discovery.worker import eval_batch
from discovery.runner import run_search, RunSummary

__all__ = ["freeze", "SnapshotMeta", "snapshot_hash", "holdout_split", "Segment",
           "HoldoutSplit", "evaluate", "run_full_scan", "segment_metrics", "metrics_of",
           "feasibility_gate", "calmar_rank",
           # Plan 2
           "normalize_params", "is_feasible", "filter_feasible", "PARAM_KEYS",
           "sample_search", "sobol_sample", "random_sample", "PARAM_SPACE",
           "eval_batch", "run_search", "RunSummary"]
```

- [ ] **Step 4: 跑测试确认通过 + 手动 run 冒烟**

```bash
.venv310/Scripts/python.exe -m pytest tests/discovery/ -v -m "not slow"   # 全 discovery 包非 slow 测试
.venv310/Scripts/python.exe -m discovery run --budget 2 --n-sobol 1 --n-random 1 --n-proc 2 --seed 42   # 手动冒烟（~6min）
```
Expected: 非slow 全 passed（含 Plan 1 既有 + Plan 2 Task 1-4 单测）；手动 run 打印 snapshot + RunSummary（n_new_trials≥1，top_inner_calmar>0），SQLite 落库。**断点续跑验证**：同命令重跑一次 → `n_skipped_dup=2 n_new_trials=0`。

- [ ] **Step 5: Commit**

```bash
git add discovery/cli.py discovery/__init__.py tests/discovery/test_cli_run.py
git commit -m "feat(discovery): cli run 命令 并发搜索跑批（Plan 2 L2+L3 集成）

- python -m discovery run：freeze+sample_search+eval_batch+run_search 全链串起
- 参数：--budget/--n-sobol/--n-random/--embargo/--n-proc/--seed/--lake-start
- 打印 RunSummary（n_sampled/n_new/n_skipped_dup/top_inner_calmar）
- 信息隔离标注 + Plan 2 无收敛判据诚实标注（留 Plan 3）
- __init__ 导出 Plan 2 API（constraints/sampler/worker/runner）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review（写完后自查，对照 spec v1.3 + Plan 1 接口）

**1. Spec 覆盖**：
- §7.1 约束裁剪 6 处耦合 → Task 1 ✓（**诚实收窄**：1/2/3/4 实现，5/6 显式标注 Plan 3——耦合5语义歧义、耦合6依赖 runtime H/ATR，非遗漏）
- §7.2 Sobol 初始覆盖 + random 补充（TPE 留 Plan 3）→ Task 2 ✓（自写 Sobol 反魔法，spec 允许"或自写"）
- §4.3 / §4.1 worker.py（ProcessPool + initializer 复用 data_lake）→ Task 3 ✓（用标准 multiprocessing.Pool，反幻觉不用未确认的 backtest/worker.py）
- §4.1 / §5.1 search 调度（采样→评估→落库→Pareto）→ Task 4 ✓（Plan 2 runner 替代 spec 的 search.py，Pareto/收敛留 Plan 3）
- §8 拷问② ProcessPool worker 崩溃单 trial 失败不影响 run → Task 3 `_eval_worker` 异常返 None + Task 4 n_failed 计数 ✓
- §8 拷问② 断点续跑去重 → Task 4 `trial_id_of`+`trial_exists` 过滤 + `INSERT OR IGNORE` 二次保险 ✓
- §3.6 算力账（单组 ~185s，吞吐 ×核数-2）→ Task 3 `n_proc=核数-2` 默认 ✓
- §6.2 信息隔离（搜索只用 inner，outer 不反馈）→ Task 4 `top_inner_calmar` 只用 inner ✓
- §3.4 SQLite 落库（Plan 1 三表 schema 复用）→ Task 4 `_persist_trial` 写 trial 表 ✓
- §3.2 trial_id = sha256(params+snapshot+seed) 去重键 → Task 4 复用 Plan 1 `trial_id_of` ✓

**2. Placeholder 扫描**：无 TBD/TODO；每步含完整 test + impl 代码 + 命令 + 预期。Task 4 `RunSummary.status="budget_exhausted"` 是**设计决策**（Plan 2 不做收敛判据，留 Plan 3），非 placeholder——显式标注 status 字段 Plan 3 扩 "converged"。✓

**3. 类型一致性**：
- Task 1 `PARAM_KEYS`（21 维 list）↔ Task 2 `_PARAM_KEYS`（从 PARAM_SPACE 派生，顺序一致）✓
- Task 2 `sample_search(n_sobol, n_random, seed) -> list[dict]` ↔ Task 4 `run_search` 调用签名 ✓
- Task 3 `eval_batch(params_list, lake_start, embargo_days, n_proc) -> list` ↔ Task 4 调用 ✓
- Task 3 `_eval_worker -> (params, result_dict) | None`，result_dict = `evaluate` 返回（`{"inner","outer","n_total"}`，Plan 1 定）↔ Task 4 解构 ✓
- Task 4 `run_search(snapshot_meta: SnapshotMeta, split: HoldoutSplit, ...)` ↔ Task 5 `cmd_run` 调用（`freeze()->(universe, SnapshotMeta)` + `holdout_split()->HoldoutSplit`，Plan 1 定）✓
- Task 4 `_persist_trial(conn, params, snapshot_meta, split_tag, engine_hash, result, source, seed)` ↔ 测试调用 ✓

**4. Windows spawn 兼容**（Global Constraints 关键）：
- Task 3 `_init_worker`/`_eval_worker` 顶层定义、无闭包、参数 str/int 可 pickle ✓（测试 `test_*_toplevel_picklable` + `test_*_not_lambda_not_nested` 守护）
- Task 3 `eval_batch` 用 `mp.get_context("spawn").Pool` 显式 ✓
- Task 5 `python -m discovery` 入口 = `discovery/__main__.py`（Plan 1 已立 `if __name__=="__main__":`），spawn 重新 import 不重复执行 ✓

**5. 反幻觉 / 反魔法**：
- 不用未确认的 `backtest/worker.py`（spec 提及但 grep 未验证接口）→ Task 3 自包含用标准库 ✓
- 不引 optuna（未安装）→ Task 2 自写 Sobol ✓
- 复用 Plan 1 接口（freeze/evaluate/store/judging）不重造 ✓

**Plan 2 显式不做（后续 plan）**：TPE 序贯优化、Pareto 前沿、收敛判据（①连续K轮/②EI<ε/④覆盖度）、schtasks 夜跑守护、runtime H/ATR 裁剪（耦合6）、suppression↔decay_tau 语义厘清（耦合5）、experiment DRAFT 闭环（L5）、多 outer 段轮流、熊市 regime 覆盖（需扩 2020-2024 数据）。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-24-discovery-l2-l3-search.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 每个 task 派 fresh subagent，task 间 review，fast iteration（适合 5 个独立 task + TDD + Windows multiprocessing 需独立验证 spawn 兼容性）。

**2. Inline Execution** - 本 session 内用 executing-plans 批量执行 + checkpoint review。

Which approach?
```
"])

Notes on key design decisions written into the plan (for the handoff summary):
- Windows ProcessPool pickle: `_init_worker`/`_eval_worker` 顶层定义 + `mp.get_context("spawn").Pool` + universe 通过 initializer 注入子进程模块全局（不随 params pickle 455MB parquet）+ 测试守护 `pickle.dumps`/`<lambda>`/`<locals>` 三条。
- 约束裁剪纯函数化: `normalize_params`（耦合1 trailing 互锁 + 耦合2 min_rr 死参）+ `is_feasible`（耦合3 tp1≤tp_h + 耦合4 cancel≥tp1/None合法）+ `filter_feasible` 整批；耦合5/6 显式收窄标注 Plan 3（语义歧义/runtime 数据，非遗漏）。
- 采样层接口: `sobol_sample(dim,n,seed)->np.ndarray`（自写 Joe&Kuo，零依赖）+ `random_sample` + `_scale_to_candidates`（单位向量→候选档，复用 param_iter.PARAM_SPACE 同源）+ `sample_search(n_sobol,n_random,seed)->list[dict]`（filter_feasible 后合法流）。
- 反幻觉/反魔法: 用标准 multiprocessing.Pool（不用未确认的 backtest/worker.py）+ 自写 Sobol（optuna 未装，spec 允许"或自写"）。
- 信息隔离: 落 inner+outer 完整记录，但 RunSummary.top_inner_calmar 只用 inner（搜索不反馈 outer，spec §6.2）。
</invoke