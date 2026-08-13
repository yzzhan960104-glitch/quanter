# P1 向量化实施计划（识别热路径 720s → ≤40s · 行为等价红线）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec §2（2026-08-12-overall-optimization-design.md）向量化颈线法识别热路径：`search_neckline` O(tops²) 双循环 → 布尔矩阵、`local_minima` O(W) 循环 → 滑窗掩码、`scan_symbol` 逐日 `iloc[:i+1]` 双切片 → 全序列数组 fast-path。行为等价红线：P0-3 等价 diff 基建 `compare()` 须 mismatches=0 且 data_hash_ok=True，等价守卫全绿。目标：单组全宇宙评估 720s → ≤40s（识别路径占 ~80% 主导，P0-1 已实证）。

**Architecture:** 单源内核设计——`method_v0.py` 新增数组内核 `_neckline_cluster`（聚集定位+压制验证）与 `_detect_core_window`（detect 全守卫），`detect_neckline_method` 重构为薄包装（公开签名不变，测试守卫 7 分支全绿），`detect_signal` 抽出 `_post_detect`（cancel_on 守卫+当日过滤+Signal 装配）与新增 `detect_signal_fast`（全序列数组+位置）共用；`backtest.scan_symbol` 改走 fast loop（预计算 arr/极值掩码/ATR/衰减权重，逐日零拷贝窗口视图）。`detect_signal`（df 路径，实盘 scan_live/scan_at 用）行为零变化。模拟层 `simulate_exit` 非 hot path（cumtime 0.3%），P1 不碰。

**Tech Stack:** Python 3.10（`.venv310`）、numpy（sliding_window_view，numpy≥1.24 已锁）、pandas。零新增依赖。

## Global Constraints

- 全中文注释，像素级说明 Why（CLAUDE.md）。
- Karpathy 极简：纯 numpy/pandas，不引入 numba/Cython/新依赖。
- **行为等价红线（spec §0.2 硬约束 5）**：P1 改造后逐信号字段级零差异——P0-3 `diag/p0_3_equivalence_diff.py::compare()` mismatches=0 + data_hash_ok=True + 等价守卫全绿。
- 等价陷阱清单（spec §2.1 + P0 交接注记补强，逐条对齐）：
  1. 极值掩码须非严格 `>=`/`<=`（search_neckline 用 `>=`、local_minima 用 `<=`，平台/并列极值要点）；
  2. 首最大 argmax 前须把 touches<min_touches 的候选 score 置 -inf（对齐旧 `score > best_score and touches >= min_touches` 的首个严格更新语义）；
  3. pandas `lows.min()`/`tail(5).mean()` 是 skipna → 数组侧用 `np.nanmin`/`np.nanmean`；
  4. 掩码有效范围 [w, n-w) 与旧 range(w, n-w) 循环一致（排除首尾各 w 根）；
  5. 衰减权重 `dt=(n-1)-ti` 是窗口相对索引；`decay_tau=None/0` 退化为等权；
  6. 日期类型契约（P0-3 deferred）：fast path 的 signal_date 由 simulate_exit 产 `datetime.date`（本改造不碰 simulate_exit，天然保持）；
  7. ENGINE_FILES 现 8 文件含 method_v0.py+backtest.py → **P1 合入后全部老 trial 失效 → 重搜基线（决策门 P1-1，须用户确认）**。
- 渐进式：每 Task 独立 commit，小步可 revert。
- file:line 基准 = 2026-08-13（本计划勘察时）。实施前 re-verify 行号漂移，以符号名定位为准。
- 测试：`.venv310/Scripts/python.exe -m pytest`（Windows，设 `PYTHONIOENCODING=utf-8 PYTHONUTF8=1`）。合并门 = 等价守卫全绿 + compare() 零 mismatch + 测速达标。

## File Structure

| 文件 | 职责 | 本计划改动 |
|---|---|---|
| `strategies/neckline/method_v0.py` | 识别层（基元/颈线/detect/detect_signal） | **核心改造**：新增 `local_extrema_mask`/`_neckline_cluster`/`_detect_core_window`/`_post_detect`/`detect_signal_fast`；`search_neckline`/`detect_neckline_method`/`detect_signal` 重构为薄包装（签名不变） |
| `strategies/neckline/backtest.py` | scan_symbol（逐标的滚动识别+去重+模拟） | scan_symbol detect 循环改 fast loop（预计算 + `detect_signal_fast`）；simulate_exit/去重/收集不动 |
| `tests/test_neckline_recognition.py` | 识别层回归（7 守卫 + 编排 + 双轨一致性） | 2 处 mock 点迁移（`nb.detect_signal` → `nb.detect_signal_fast`，orchestration + dual-track）；其余不动 |
| `tests/test_p1_fast_path.py`（新建） | P1 fast path 专项：掩码/内核/fast==df 等价 | **新建**（TDD） |
| `diag/p1_1_speed.py`（新建） | P1 测速对拍（before/after 同口径） | **新建**（Task 0 已录 before 基线） |
| `docs/superpowers/plans/2026-08-13-p1-vectorization.md` | 本计划 | — |

## Tasks

### Task 1：基元向量化——local_extrema_mask + _neckline_cluster + search_neckline 重构（TDD）

**物理意图：** `local_minima`/`local_maxima` 逐点 Python 循环（O(n×W)）与 `search_neckline` 内联 tops 检测 + O(tops²) 双循环是 P0-1 top-5 热点。新增全序列向量化掩码 `local_extrema_mask`（sliding_window_view 双窗 max/min 比较），聚集聚类改布尔矩阵 `D = |vals[:,None] - vals[None,:]| <= atr`。

**Files:**
- Modify: `strategies/neckline/method_v0.py`（新增 `import numpy as np` + `from numpy.lib.stride_tricks import sliding_window_view`；新增 `local_extrema_mask`/`_neckline_cluster`；`search_neckline` 重构为薄包装）
- Test: `tests/test_p1_fast_path.py`（新建）——TDD 先写：
  - `test_local_extrema_mask_matches_local_minima`：掩码 True 位 == 旧 `local_minima(lows, w)` 的位置集合（合成含平原/并列极值）
  - `test_local_extrema_mask_matches_local_maxima`：同上（kind="max"）
  - `test_local_extrema_mask_short_series`：n < 2w+1 → 全 False
  - `test_neckline_cluster_equivalent`：`_neckline_cluster` 与旧 search_neckline 同输入同输出（含 decay_tau 分支 + 首最大平局场景）
- Gate: `tests/test_neckline_recognition.py` 现有 `test_search_neckline_finds_cluster`/`test_search_neckline_reject_few_tops` 全绿。

**Equivalence details（逐位对齐旧实现）:**
- 掩码：`sw = sliding_window_view(arr, w)`；left = sw[:n-2w].min/max(axis=1)（i ∈ [w, n-w) 的 i-w 窗），right = sw[w+1:n-w+1].min/max(axis=1)（i 的 i+1 窗）；`mask[w:n-w] = (vals >= left) & (vals >= right)`（max 用 >=，min 用 <=）。
- 聚类：`tops_pos = flatnonzero(mask)`；`len < min_touches → (None, 0.0)`；`D = abs(vals[:,None]-vals[None,:]) <= atr`；`touches = D.sum(axis=1)`；decay 权重 `w_t = decay_weights[tops_pos]`（decay_weights[i]=exp(-((n-1)-i)/tau)，n=len(highs_w)）；`scores = D @ w_t`；`scores[~valid] = -inf`；`best = argmax`（首最大）；压制时长同窗口布尔和/加权和。

### Task 2：_detect_core_window + detect_neckline_method 薄包装重构

**物理意图：** detect 的 7 守卫全逻辑下沉到数组内核 `_detect_core_window`（窗口切片视图 + 窗口相对掩码），`detect_neckline_method(df, cfg, atr_series)` 保持公开签名，改薄包装：tail(window) → 提取 .values → 现场算掩码 → 调内核。fast path 与 df 路径共享同一内核（识别单源）。

**Files:**
- Modify: `strategies/neckline/method_v0.py`
- Test: `tests/test_p1_fast_path.py` 增 `test_detect_core_window_matches_detect`（合成形态：内核 == detect_neckline_method 输出 dict 逐字段，含 7 守卫全过路径）
- Gate: `tests/test_neckline_recognition.py` 全部 detect 测试（成功 + 5 拒绝边界 + R3 rr/stop_price）全绿。

**Equivalence details:**
- 内核顺序对齐旧 detect：ATR 检查 → 颈线聚集+压制 → 底部（`np.nanmin` + 掩码值过滤 [min, min+ATR] → `bottom_set` round(…,4)）→ 突破 close_T → 带量 vol5（`np.nanmean(vols_w[-5:])`）→ H/深度/止盈/止损/rr → 返回 dict（字段与 round 与旧逐位一致，formed_at=index_w[-1]）。
- `lows.min()` skipna ↔ `np.nanmin`；`tail(5).mean()` skipna ↔ `np.nanmean`。

### Task 3：_post_detect 抽取 + detect_signal 重构 + detect_signal_fast 新增

**物理意图：** cancel_on 守卫 + 当日突破过滤 + Signal 装配从 `detect_signal` 抽成 `_post_detect(symbol, res, exec_cfg, date, close_T, atr_last)`，`detect_signal`（df 路径，公开签名不变）与 `detect_signal_fast`（数组路径）共用——装配逻辑单源，防"研究侧 fast path 与实盘 df 路径装配分叉"。

**Files:**
- Modify: `strategies/neckline/method_v0.py`
- Test: `tests/test_p1_fast_path.py` 增：
  - `test_detect_signal_fast_equals_detect_signal`：合成突破形态，`detect_signal(df)` == `detect_signal_fast(arr, pos=len-1)`（Signal 逐字段）
  - `test_detect_signal_fast_cancel_on`：fast path cancel_on 守卫同 detect_signal
- Gate: `tests/test_detect_signal.py` 五分支全绿（stub 仍打在 detect_neckline_method 上）。

**Equivalence details:** `_post_detect` 逐位保持原 detect_signal 后半段：`atr_use = float(atr_last) if not isna else (res.get("atr") or 0.0)`（entry 回退带 or 0.0）、`atr = float(atr_last) if not isna else res.get("atr")`（atr 字段回退不带 or）——两处回退口径不同，勿合并。

### Task 4：scan_symbol 改 fast loop + 迁移 2 处 mock 点

**物理意图：** `scan_symbol` 逐日 `sym_df.iloc[:i+1]` + `atr_full.iloc[:i+1]` O(n²) 拷贝是识别路径占比 ~80% 的主因（P0-1）。改：预算 ATR 后一次性预计算 arr（H/L/C/V/index 数组）、tops 掩码（w=3）、bottoms 掩码（local_extrema_window）、decay_weights；循环内 `detect_signal_fast(None, arr, i, id_cfg, exec, sym_df.index[i], atr_arr, ...)`（窗口切片=零拷贝视图）。去重/simulate_exit/收集段不动。

**Files:**
- Modify: `strategies/neckline/backtest.py`（import 增 `detect_signal_fast`/`local_extrema_mask`/`np`；scan_symbol detect 循环替换）
- Modify: `tests/test_neckline_recognition.py`（`test_scan_symbol_orchestration` 与 `test_scan_symbol_matches_strategy` 的 mock 点：`nb.detect_signal` → `nb.detect_signal_fast`，fake 判据 `len(d)==signal_idx+1` → `pos==signal_idx`；dual-track 场景 nm.detect_signal 桩保留）
- Gate: `tests/test_neckline_recognition.py` 编排/双轨测试全绿；`tests/test_neckline_core.py::test_scan_symbol_forwards_id_cfg`（真实 lake 数据端到端）全绿；`tests/test_param_iter_kernel_same_source.py` 全绿。

### Task 5：等价验收——compare() + 全量等价守卫 + golden

**Files:** 无生产改动；执行验收：
1. `diag/p0_3_equivalence_diff.py` 跑 compare()：mismatches=0 且 data_hash_ok=True（15 信号/10 标的锚）。
2. 等价守卫全量：`tests/test_neckline_recognition.py` + `tests/test_neckline_core.py` + `tests/test_detect_signal.py` + `tests/test_param_iter_kernel_same_source.py` + `tests/strategies/` + `tests/test_p1_fast_path.py`。
3. golden 回归：`backtest/tools/regression_neckline_golden.py`（端到端对拍）。
4. `diag/p1_1_speed.py` 重跑 after 数字（段3 设 `P1_FULL_UNIVERSE=1` 全宇宙验收 ≤40s）。

**Gate:** 全绿 + mismatches=0 + data_hash_ok=True + 段3 [PASS]。

### Task 6：测速汇总 + P1-1 决策门注记

**Files:** 本计划文档 §实测结果 填充 before/after 数字；决策门 P1-1（engine_hash 重搜基线）在 commit message 与交付说明中显式标注「须用户确认：P1 合入 → 全老 trial 不可比 → 需重搜基线 + 暂停跨夜 daemon」。

---

## 实测结果（执行时填充）

### before（旧实现，Task 0 录 · 2026-08-13）
- 段1 10标的：1.50s（**149.8ms/标的**）；段2 100标的：13.91s（139.1ms/标的）
- 全宇宙 720s/组（discovery daemon.py:37 实测锚，1334 标的时代口径）

### after（向量化后，Task 5 录 · 2026-08-13）
- 段1 10标的：0.31s（**31.5ms/标的**）；段2 100标的：2.97s（29.7ms/标的）→ per-symbol **~4.7x 加速**
- 段3 全宇宙 run_full_scan：**35.47s / 1135 signals → 验收门 ≤40s [PASS]**（spec §2.3）
- compare()：mismatches=**0**（须 0）data_hash_ok=**True**（须 True），15/15 信号逐字段零差异
- 等价守卫全量：**135 passed**（test_p1_fast_path 22 + neckline_recognition 20 + detect_signal 5 +
  neckline_core 16 + param_iter_kernel_same_source 2 + strategies 9 + decide_exit + backtest 系列）
- 剩余热点（P1 验收门已过，记录供 P2+ 参考）：per-symbol 仍 ~30ms，大头转移为
  `compute_atr`（pandas rolling 全序列，~数 ms/标的）与 detect 内核逐日 Python 帧开销——
  非 P1 范围（P1 只打识别循环，simulate_exit 非 hot path 不碰）。

### golden 回归发现（P1 无关的预先存在陈旧）
- `backtest/tools/regression_neckline_golden.py --verify` 在 EXEC_DEFAULTS 哈希校验处 FAIL：
  golden 基线 2026-07-22 捕获，之后 Task 12（P1-9 交易成本对齐实盘）合法新增
  `commission_rate/stamp_rate/transfer_rate` 3 键 → 哈希漂移。DEFAULTS 哈希校验通过
  （P1 未动 DEFAULTS 内容 ✓）。**P1 等价性不由该 golden 证明**（其数值口径已过时），
  由 P0-3 冻结基线 compare() 证明。golden 重新捕获属独立小修，不在本计划范围。

## 决策门

- **P1-1（engine_hash 重搜基线）**：P1 触碰 ENGINE_FILES 中 method_v0.py + backtest.py → engine_hash 变 → 全部老 trial 不可比。裁定：P1 验收通过后**暂停跨夜 daemon → 一次性重搜基线**（新 snapshot 合法起点）。须用户确认后执行（不随本计划自动执行）。
