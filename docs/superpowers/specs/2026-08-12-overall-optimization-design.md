---
title: quanter 策略与参数探索整体优化设计（P0-P6）
date: 2026-08-12
revision: v1（2026-08-12 落盘：基于 64 新提交复查，P6 重定位为验收+delta、湖深已实证、P3 可视化进后台）
status: draft（待用户审阅）
author: session（peer reviewer / risk officer 姿态）
scope: 颈线法策略本体 + discovery 参数探索架构整体优化（性能 / 方法论 / 可分析性 / 数据可信度四支柱）
related:
  - docs/2026-08-11-framework-evaluation-decision.md（框架评估决策：Qlib+五框架全部不引入）
  - plans/wayfinder/T13-blueprint.md（P6 引用：data 完整性治理蓝图，T13-A/B 已实现）
  - docs/architecture/06-tech-debt.md（债务切片）
  - docs/superpowers/specs/2026-08-11-tech-debt-governance-master-design.md（技术债总纲）
  - docs/superpowers/plans/2026-08-12-w0-tail.md / 2026-08-11-m4-test-hygiene.md（并行计划，恢复执行前核对 git status）
---

# 策略与参数探索整体优化设计

> 本 spec **不改代码**；改造由后续实施计划（writing-plans）落地。每个阶段独立验收门，可随时暂停交付。
> 终态产物 = 本 spec + 各阶段实施计划 + 每项策略改动的 ADR 实证记录。

---

## 0. 范围、硬约束、现状基线

### 0.1 治理点清单（本 spec 覆盖）

| ID | 支柱 | 内容 | 状态 |
|---|---|---|---|
| P0 | 基线 | cProfile 热点确认 + 湖深细节核 + 等价守护盘点 + RSS 基线 | 待做（湖深主体已实证） |
| P1 | 性能 | 识别热路径向量化（720s → ≤40s，行为等价） | 待做 |
| P2 | 性能 | n_proc 放开 + TPE batch 并行（吞吐 ~80x） | 待做 |
| P3 | 可分析性 | 敏感性分析 + 热力图 + 报告 → **后台可视化（DiscoveryLabView）** | 待做 |
| P4 | 策略本体 | 死参数降维 + 语义改进（one-at-a-time + inner 实证） | 待做（P3 数据驱动） |
| P5 | 方法论 | walk-forward 多折 + 分折 universe 重建 + DSR 排序升级 | 待做 |
| P6 | 数据可信度 | **重定位：验收 T13-A/B 已有实现 + 只做两个 delta** | 验收+delta |
| 决策 | 框架 | Qlib + vnpy + WonderTrader + Backtrader + VectorBT + NautilusTrader 全部不引入 | ✅ 已定（见 related） |

### 0.2 硬约束（CLAUDE.md + MAP，不可违背）

1. 全中文 + 像素级注释（含 Why）。
2. Karpathy 极简：零新增依赖优先；"允许架构级改动"仅限 P2 并行模型与 P1 向量化（numpy/pandas 内），不引入重型黑盒。
3. 量化风控红线：无前视（策略层）/ inner-outer 信息隔离（discovery）/ 宏观 ffill-only。
4. **行为等价是红线**：P1 向量化必须逐信号字段级 diff 零差异（golden 守卫 + 抽样宇宙对比脚本）；P4 语义改动走 ADR 实证，不改等价红线的已测路径。
5. 渐进式：每阶段独立 commit，可暂停可 revert。

### 0.3 现状基线（勘察实证，2026-08-11 探索 + 08-12 复查）

- **瓶颈**：单组全宇宙评估 ≈720s/12min。热点 = `scan_symbol` 逐日 `sym_df.iloc[:i+1]` + `atr_full.iloc[:i+1]` 双切片（strategies/neckline/backtest.py:467-468）+ `search_neckline` O(tops²) 纯 Python 双循环（method_v0.py:151-164）+ `local_minima` O(W) 循环；`scan_at`（strategy.py:139-140，replay 引擎路径）同构 O(T²)。每 eval ≈ 50 万次 detect 调用。
- **不可迁移资产**：① 回测执行模拟的实盘等价性（decide_exit 单源）② discovery 方法论（DSR/信息隔离/断点续跑/跨夜收敛）③ 已建成实盘引擎（T1 拆分完成）④ 数据指纹体系（snapshot hash / engine_hash）。
- **08-12 复查（64 新提交 f0a6431c→96d55418）**：`discovery/` `strategies/` `backtest/` **零改动** → P0-P5 假设全部成立；T13-A（write-side safety net）/ T13-B（scan repair loop）已实现 → P6 重定位；湖深已实证（10,218,475 行 / 2016-07-06 ~ 2026-08-07，T13-blueprint 地基核实）→ walk-forward 数据可行；T2 keystone 为纯 trading 层手术，P4 无新增交互。

---

## 1. P0 基线测量（恢复执行第一步，全只读，半天）

| 项 | 内容 | 产出 |
|---|---|---|
| P0-1 | cProfile 单标的全序列 scan_symbol + 抽样 30 标的 → 确认热点分布 | 热点函数耗时表（预期：search_neckline + iloc 切片 + local_minima） |
| P0-2 | 湖深细节核（湖深主体已实证）：创板科创逐年覆盖数 + 退市标的痕迹 | walk-forward 分折 universe 可行性确认 |
| P0-3 | 等价守护盘点：test_scan_symbol_matches_strategy / test_detect_signal_precomputed_atr_equivalent / golden 回归清单 + 建抽样 universe 新旧 diff 脚本 | P1 验收基线 |
| P0-4 | 每 worker RSS 基线（discovery worker 与 replay worker 各测） | P2 n_proc 上限公式输入 |

验收门：P0-1 热点占比 ≥70% 落于预期函数；P0-2 结论可支撑 P5 分折定义。

---

## 2. P1 评估热路径向量化（性能 20-50x，行为等价红线）

### 2.1 设计（公开签名不变）

- **每 symbol 一次预计算**：numpy 数组（H/L/C/V）+ 局部极值掩码。
  - tops 掩码：`search_neckline` 的 `top_window=3` **硬编码**（cfg 不可调）→ 固定 w=3 全序列掩码；
  - bottoms 掩码：`local_extrema_window` 参数（∈{3,5}）→ 按参数惰性缓存（每 eval 每 symbol 一次，微秒级）。
- **窗口边界语义**：有效 tops 位置 = 窗口相对 [3, n-4]（`range(top_window, n-top_window)`），全序列掩码按窗口起点偏移。
- **聚集聚类向量化**：tops×tops 外层 diff ≤ ATR 布尔矩阵 → 计数/衰减加权 score；首最大语义（严格 `>` 更新）与 `np.argmax` 首最大一致。
- **消除逐日切片**：`detect_signal` 增加 position 化 fast-path（全量数组 + 位置 i）；`scan_symbol`/`scan_at` 改走 fast-path（ATR 已预算，只需标量末值）。
- **等价性陷阱清单**（向量化必须逐一对齐）：
  - pandas Series `.min()/.mean()` **skipna**（`lows.min()`、`tail(5).mean()`）→ `np.nanmin`/`np.nanmean`；
  - numpy ndarray `.max()/.min()` **NaN 传播** → 局部极值比较天然一致，勿混用 pandas 方法；
  - 衰减权重 `dt=(n-1)-ti` 是**窗口相对索引**；
  - Python `round` 与 `np.round` 均银行家舍入 → 一致；
  - suppression decay 加权求和、cancel_on close 守卫语义不变。

### 2.2 engine_hash 交互（🚪 决策门 P1-1）

ENGINE_FILES（compute_unit/hashes.py）含 `strategies/neckline/backtest.py` + `method_v0.py` → **P1 合入后全部老 trial 不可比**。
决策：P1 验收通过后立即重搜基线（新 snapshot 合法起点）；P1 期间暂停跨夜 daemon（或接受一次性快照重置）。需用户确认。

### 2.3 验收门

- 等价守卫全绿（tests/strategies + golden）+ 抽样 universe 新旧实现逐信号字段级 diff **零差异**；
- 单组全宇宙评估实测 720s → ≤40s（记录实测值）；
- 吞吐预估：4h/夜 × 3600s ÷ 25s × 12 进程 ≈ 6900 trial/夜（当前 ≈80，~80x）。

---

## 3. P2 吞吐模型升级（架构级）

| 项 | 内容 |
|---|---|
| n_proc 放开 | P1 后重测每 worker RSS + `read_parquet` 列裁剪（只取 OHLCV+amount）→ 32GB 机器目标 8-16 进程 + **RSS 看门狗**（超阈值自动降并发，防 2026-08-03 MemoryError 复发） |
| TPE batch 并行 | `discovery/search.py` 改 optuna 原生 `ask(n_batch)/tell` 批量 → ProcessPool 并行 evaluate（runner.py:158-177 现为主进程串行）；`expected_improvement` 收敛判据兼容保持 |

验收门：相同 seed 下 batch-TPE 与串行 TPE 收敛趋势对比测试；新 n_proc 下 30 分钟压测无 MemoryError。

---

## 4. P3 可分析性 → 后台可视化（直接看）

### 4.1 数据流

```
logs/discovery_trials.db（snapshot/trial/search_run 三表）
  → discovery/sensitivity.py（新模块：边际效应/方差分解/覆盖度，纯读库零写入）
  → presentation/server/api/v1/research.py（新只读端点）
  → Vue 新视图 DiscoveryLabView.vue（echarts 5.5 已有，零新依赖）
```

分层红线：discovery 包零 presentation 依赖（与 discovery_bridge 同款：research 层读库、discovery 纯函数）。

### 4.2 端点（3 个只读）

| 端点 | 内容 |
|---|---|
| `GET /api/v1/research/discovery/sensitivity` | 21 维敏感性表：各参数档 inner calmar/胜率/回撤均值（边际效应）+ 方差分解主效应排名 + 死参数标记 + 覆盖度 ρ 与盲区提示 |
| `GET /api/v1/research/discovery/heatmap?x=&y=&metric=&fill=false` | 两维网格：`{x_axis, y_axis, grid, n_obs}`（n_obs 同行返回防"单点热区"误导）；`fill=true` 补格依赖 P1，默认关 |
| `GET /api/v1/research/discovery/params` | PARAM_SPACE + 耦合约束元数据（前端维度选择器联动） |

⚠ 接入注意：`research_router` 现挂 `require_write`（main.py:661），新端点纯只读 → 单独挂载去 write 依赖（与 logs/macro 同款）。

### 4.3 前端 DiscoveryLabView.vue

独立视图（ParamLab 是训练 loop 写交互，本视图是搜索分析只读）：① 敏感性仪表板（边际效应条形图 + 死参数徽标 + 盲区警告）② 热力图（x/y/指标 三维选择器 + 样本量角标）③ 搜索进展（现有 discovery/status + 覆盖度指示器）。配套路由 + 侧边栏入口 + `@/api/discovery.ts`（沿用 `@/api/caisen` 模式）+ `qt-card` 样式。

### 4.4 验收门

已知死参数 min_rr 在敏感性面板输出"低方差"正确结论；后端 `tests/discovery/test_sensitivity.py` + tests/server 契约测试；前端 vue-tsc + vitest（沿用 ParamLabView.spec.ts 模式）；视图可打开无权限问题。

---

## 5. P4 策略本体改进（P3 数据驱动，语义变更需 ADR）

### 5.1 协议（防因果混淆）

- 单参数 one-at-a-time + inner 实证对比（基线 vs 改动，同 snapshot 同 universe）；
- 只合入显著正收益项；每项产出 ADR 记录（实证数字 + 结论）；
- **每次合入 = engine_hash 变更 = 触发重搜**（与 P1-1 同机制）。

### 5.2 候选方向（代码锚点，P3 数据确认优先级后动手）

| 候选 | 锚点 | 说明 |
|---|---|---|
| 颈线"量加权"替代时间衰减 | method_v0.py:55-59 注释 | decay 方案A 净-3.2 点教训，"量加权或其他"留坑 |
| suppression 等权 vs 衰减口径 | method_v0.py:168-177 | 近期颈线漂移问题 |
| 止盈标尺统一 | backtest.py:146-147 | tp 用 H 几何、止损用 ATR 波动，两标尺混用合理性 |
| 死参数降维 | constraints.py:33（min_rr） | P3 方差分解后，PARAM_SPACE 21 → ~18-19 维 |

验收门：每项改动有 inner 实证对比记录；无 P3 数据支持的改动一律不做。

---

## 6. P5 搜索方法论升级（walk-forward + 排序）

### 6.1 walk-forward（数据可行性已实证：湖 2016-2026）

- `discovery/split.py` 增 `WalkForwardSplit`：训练折 2020-21 / 22-23 / 24 / 25 + 终局 OOS 2026；
- **每折重建 universe**（流动性/可交易过滤按折末 30 日重算，防幸存者偏差；吸收 Qlib point-in-time instrument 设计思想）；
- 交易日历用 `data/calendar.py`（现成资产），embargo 机制沿用；
- 快照指纹扩展含分折定义（snapshot hash 变化 → 跨夜收敛显式重置，机制已存在）。

### 6.2 DSR 排序升级（🚪 决策门 P5-1）

L1 calmar 排序基础上，DSR 从"标注"升级为排序共因子（calmar × DSR 门槛或双目标 Pareto）。前置验证：历史 trial 语料回放**不空集、不惩罚早期搜索**，再出 mini 决策记录（保留 ADR13 诚实报告原则）。

验收门：walk-forward 与现有二段 holdout 在相同 params 下交叉验证一致性；DSR 排序回放不空集。

---

## 7. P6 数据可信度：验收 + delta（重定位，不重复造轮子）

### 7.1 已有实现（T13-A/B，验收不重写）

| 原计划项 | 现状实现 |
|---|---|
| 历史连续性校验 | `data/integrity.py` 停牌区间重建 + `find_gaps` + `GapRange` |
| 交易日历基准 | `data/calendar.py`（Tushare trade_cal + 本地缓存 + weekday 兜底） |
| 自动补采 | `data/tools/repair_gaps.py` + `scan_integrity` |
| 写入守卫 | T13-A write-side safety net（safe_overwrite） |
| 过滤告警 | W0-tail 计划进行中（engine 调用方节流告警） |

### 7.2 本计划 delta（仅两项）

- **D6-1**：T13-B 生产 gate 接线（L2 检测侧——freshness 只校验 latest_date 不校验连续性的缺口，接线到生产路径）；
- **D6-2**：discovery snapshot 指纹联动连续性状态（T13 蓝图三层缺陷模型未覆盖 discovery 侧；数据补齐导致 hash 变化 → 跨夜收敛重置）。

验收门：注入人工缺口 → 检出 + 补采恢复；补采后 snapshot hash 变化触发重置（机制已存在，验证联动）。

---

## 8. 参数方案最终形态（治理闭环）

"最终参数"不是预设值，是更严的搜索-验证-发布闭环产出：

```
P1 向量化（21 维语义零变化）→ 重搜基线（engine_hash 变更）
P3 敏感性 → P4 降维决策（数据驱动：min_rr 等死参数移出搜索，21 → ~18-19 维）
P4 语义改进（量加权等，ADR 实证）→ 每次合入触发重搜
P5 walk-forward 多折验证（冠军须跨折稳定）→ outer 2026 终局去偏
publish DRAFT（weight=0）→ 人审 promote → 新 ACTIVE 参数
```

交付物：新 ACTIVE 参数表（格式同 README §3.2）+ 每项 P4 改动 ADR + walk-forward 折间稳定性记录。当前 ACTIVE（neckline_disc_20260725_25c602）将被新一轮冠军取代。

---

## 9. 风险与护栏

| 风险 | 缓解 |
|---|---|
| P1 向量化破坏等价 | golden 守卫 + 抽样 universe 字段级 diff；任何 diff 即回退定位 |
| engine_hash 失效 | P1 完成即重搜基线（P1-1 决策门）；P4 每次语义改动配套重搜；daemon 跨夜期间暂停策略代码改动 |
| 幸存者偏差污染 walk-forward | 每折独立重建 universe（含退市标的处理，P0-2 核实） |
| 内存复发（2026-08-03 教训） | RSS 看门狗 + 列裁剪 + 进程数按实测 RSS 公式 |
| 多改动叠加因果混淆 | P4 one-at-a-time 协议，每项独立 inner 实证 ADR |
| TPE batch 改变搜索动力学 | 相同 seed 串行/批量对比，batch 大小可调 |
| 与并行计划撞文件 | 恢复执行前核对 git status（w0-tail / m4-test-hygiene 未跟踪计划） |

---

## 10. 执行顺序建议

1. P0 先行（半天，全只读）→ 修正 P1 收益预期与 P5 分折定义；
2. P1 里程碑（性能大头 + 零语义风险，验收即重搜基线）；
3. P2/P3 紧随；P4 依赖 P3 数据；P5/P6 可与 P3 并行；
4. 每阶段独立验收，可随时暂停交付。
