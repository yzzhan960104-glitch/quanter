# P2 吞吐模型升级实施计划（TPE batch 并行 + n_proc 放开 + RSS 看门狗）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec §3（2026-08-12-overall-optimization-design.md）升级发现引擎吞吐：① TPE 阶段从主进程串行 evaluate 改为 optuna ask/tell 批量并行（每轮 ask K → 进程池批量评估 → 统一 tell）；② n_proc 放开（cap 4 → 16，20 核机器）；③ RSS 看门狗（自动降并发 + fail-loud 防 2026-08-03 MemoryError 复发）；④ daemon 夜预算常数按 P1 实测 35s/组 更新（720s 旧锚过时）。目标：4h 夜 × 12-16 进程 ≈ 数千 trial/夜（当前 ~80）。

**Architecture:** worker.py 增 `EvalPool`（长驻池：TPE 多轮复用同一批 worker，每 worker freeze 一次）+ `memory_cap_n_proc`（psutil 可用内存 ÷ 每 worker RSS 估计，P0-4 实测 0.57GB/worker）+ `_init_worker` 内 RSS fail-loud 看门狗；search.py 增 `tpe_search_batch`（seed 点直接 tell 已知值，TPE 新采 ask 批量 → evaluate_batch_fn 注入 → tell 值/FAILED 态）；runner.py 阶段二改走 batch（阶段一 eval_batch 不动，测试 seam 最小化）。`tpe_search` 串行版保留（公开 API + 测试锚）。

**Tech Stack:** Python 3.10、optuna 4.9.0（ask/tell 官方模式）、psutil（requirements.txt 已有，P0-4 引入）。零新增依赖。

## Global Constraints

- 全中文注释，像素级说明 Why（CLAUDE.md）。Karpathy 极简：不引入新依赖。
- **行为等价红线**：P2 只改搜索执行模型（TPE 采样顺序/并发），不改识别/模拟内核——evaluate 语义零变化；engine_hash 不变（不触碰 ENGINE_FILES）。
- 信息隔离（spec §6.2）不变：TPE 目标仍 = inner calmar，outer 只进报告。
- 渐进式：每 Task 独立 commit。
- file:line 基准 = 2026-08-13。实施前 re-verify，以符号名定位为准。
- 测试：`.venv310/Scripts/python.exe -m pytest`（`PYTHONIOENCODING=utf-8 PYTHONUTF8=1`）。合并门 = tests/discovery 全绿 + 串行/批量同 seed 收敛趋势对比 + 实际跑批 smoke。

## File Structure

| 文件 | 职责 | 本计划改动 |
|---|---|---|
| `discovery/worker.py` | 采样层并发（Pool/initializer/_eval_worker） | `_default_n_proc` cap 4→16；新增 `memory_cap_n_proc`/`EvalPool`；`_init_worker` 加 RSS fail-loud 看门狗；`eval_batch` 接内存 cap |
| `discovery/search.py` | L3 搜索（TPE） | 新增 `tpe_search_batch`（ask/tell 批量）；`tpe_search` 串行版保留 |
| `discovery/runner.py` | 跑批主循环 | 阶段二改 batch：EvalPool + tpe_search_batch + 失败 FAILED 态；删主进程 freeze/_res_cache（不再需要） |
| `discovery/daemon.py` | 跨夜编排 | `estimate_budget` 的 per-trial 常数 720s → 40s（P1 实测 35.5s，留余量） |
| `tests/discovery/test_worker.py` | worker 单测 | cap 测试更新（4→16）+ memory_cap + EvalPool 复用测试 |
| `tests/discovery/test_search.py` | 搜索单测 | tpe_search_batch 3 例（找峰/同 seed 趋势/失败态） |
| `tests/discovery/test_runner.py` | runner 单测 | `_mock_search_deps` 的 TPE seam 迁 tpe_search_batch + worker.EvalPool fake |
| `docs/superpowers/plans/2026-08-13-p2-throughput.md` | 本计划 | — |

## Tasks

### Task 1：worker.py n_proc 放开 + 内存 cap + RSS 看门狗 + EvalPool（TDD）

**物理意图：** P0-4 实证内存非瓶颈（0.57GB/worker，49 上限）→ cap 4 是过时约束（720s 时代口径），放开到 CPU 约束（20 核 → 16）。但"数据膨胀/列裁剪失效"回归会瞬间复现 2026-08-03 场景——内存公式自动降并发（评估期每 worker RSS 估计 × 可用内存）+ worker 侧 RSS fail-loud 双保险。

**Files:**
- Modify: `discovery/worker.py`
- Modify: `tests/discovery/test_worker.py`（`test_default_n_proc_capped_at_4` → cap 16；新增 `test_memory_cap_n_proc_clamps`（monkeypatch psutil.virtual_memory）、`test_eval_pool_reuses_workers`（真实 spawn，两次 eval 同结果））
- Gate: tests/discovery/test_worker.py 全绿（含既有真实 Pool 测试）

**接口：**
- `_default_n_proc()`：env 覆盖 > min(16, cpu-2)；常量 `_N_PROC_CPU_CAP = 16`
- `memory_cap_n_proc()`：`(psutil 可用内存 − 4GB 储备) ÷ DISCOVERY_WORKER_RSS_GB(默认1.0)`，psutil 不可用降级返 CPU cap
- `EvalPool(n_proc=None, lake_start, embargo_days)`：`.eval(params_list)` / `.close()`；构造时 `min(n_proc, memory_cap)`
- `_init_worker` 看门狗：freeze 后 psutil 量 RSS > `DISCOVERY_WORKER_RSS_MAX_GB`(默认6) → stderr CRITICAL + `os._exit(3)`（fail-loud）；看门狗自身异常吞掉（测量失败降级）

### Task 2：search.py tpe_search_batch（ask/tell 批量，TDD）

**物理意图：** TPE 序贯需前序结果约束被 ask/tell 官方并行模式松绑——每轮 ask K 个候选（TPE n_startup_trials 默认 10 兜底零完成态冷启），并行评估后统一 tell。seed（Sobol 阶段一已评估）直接 tell 已知 calmar（旧串行版经 objective 重估，浪费一轮评估）。

**Files:**
- Modify: `discovery/search.py`
- Modify: `tests/discovery/test_search.py` 新增：
  - `test_tpe_search_batch_finds_peak`：合成峰形 objective（峰在特定候选档），batch 收敛到峰邻域
  - `test_tpe_search_batch_matches_serial_trend`：同 seed 串行 vs 批量，best_value 与真峰差同量级（收敛趋势一致，非逐位同）
  - `test_tpe_search_batch_failed_eval_marks_failed`：evaluate 返 None → pair 含 None + study 该 trial FAILED 态
- Gate: tests/discovery/test_search.py 全绿（既有 9 例不动）

**接口：**
```python
def tpe_search_batch(seed_params, seed_values, evaluate_batch_fn, n_trials=20,
                     seed=42, param_space=None, batch_size=8) -> (new_pairs, study)
# new_pairs = [(params, result|None), ...] 仅 TPE 新采；result None = 评估失败（调用方过滤）
# evaluate_batch_fn(list[dict]) -> list[result|None]（调用方注入 EvalPool.eval）
```

### Task 3：runner.py 阶段二切 batch

**物理意图：** 阶段二不再主进程串行 evaluate/freeze——EvalPool 长驻（TPE 多轮复用 worker，避免每轮重 spawn+重 freeze），seed 用阶段一新鲜 (params, result) 对（无 dup 重估），失败 trial 计 n_failed 不落库。

**Files:**
- Modify: `discovery/runner.py`（阶段二块重写；阶段一 eval_batch 不动；`from discovery.objective import evaluate` 删——evaluate_replay 保留）
- Modify: `tests/discovery/test_runner.py`（`_mock_search_deps`：tpe/evaluate patch 换 tpe_search_batch + `discovery.worker.EvalPool` fake）
- Gate: tests/discovery/test_runner.py 全绿（12 例）

### Task 4：daemon 夜预算常数 + 验收

**Files:**
- Modify: `discovery/daemon.py`（`estimate_budget` per-trial 720 → 40 注释订正）
- 验收：tests/discovery 全量绿 + `discovery run` 实际 smoke（小预算 batch TPE 跑通落库）+ 与 P1-1 基线重搜交叉验证（trial 落库 engine_hash 一致）

---

## 实测结果（执行时填充）

- n_proc 默认：_待填_（20 核机器）
- memory_cap：_待填_（32GB 机器）
- batch TPE smoke：_待填_（budget/tpe_trials/耗时/落库数）
- 同 seed 串行 vs 批量收敛：_待填_

## 风险与护栏

| 风险 | 缓解 |
|---|---|
| batch ask 候选重复（TPE 未见新 tell 时） | optuna ask 官方并发模式（trial.number 各异采样）；落库 trial_id 去重兜底 |
| 内存回归（数据膨胀） | memory_cap_n_proc 自动降并发 + worker RSS fail-loud + env 可调 |
| runner 测试 seam 断裂 | 阶段一 eval_batch seam 不动；TPE seam 显式迁移并同步测试 |
| TPE 动力学改变 | 同 seed 串行/批量收敛趋势对比测试（验收项） |
