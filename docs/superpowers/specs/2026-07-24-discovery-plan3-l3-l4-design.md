# 参数发现引擎 · Plan 3（L3 搜索层完成 + L4 自治层核心）实施设计补充

> 日期：2026-07-24
> 父 spec：`docs/superpowers/specs/2026-07-23-param-discovery-engine-design.md` v1.3
> 前置：Plan 2（L2 吞吐 + L3 采样基础）已交付，commit `12665ca..3b6fe17`（feat/discovery-l0-l1）
> 实施 plan：由 writing-plans 基于本文档展开（`docs/superpowers/plans/2026-07-24-discovery-l3-l4-convergence.md`）

## 1. 定位与范围

Plan 2 已立"Sobol 初始覆盖 + ProcessPool 并发 + 断点续跑去重"的跑批骨架，但 `RunSummary.status` 恒 `"budget_exhausted"`——**run 不会自停**，夜跑会空烧算力；且缺 Pareto 前沿、收敛判据、统计裁决，搜索结果无法判"是否真收敛"。

Plan 3 在 Plan 2 之上补齐 spec 的：
- **L3 搜索层完成**（§7.2 阶段二）：optuna TPE 序贯优化（Sobol 初始覆盖已由 Plan 2 立）
- **L4 自治层核心**（§3.5 + §4.1 + §3.7）：Pareto 非支配前沿 + 收敛判据①②③④ + DSR 统计裁决

让 run 能**可信自停**（判据④覆盖度前置否决防伪收敛），搜索结果带**统计显著性**标注（DSR）。

### 做（Plan 3 范围）
Pareto 前沿、收敛判据①连续K轮/②EI<ε/③预算耗尽/④覆盖度、覆盖度度量、optuna TPE、DSR、耦合5语义厘清、耦合6 runtime 裁剪、runner 自停、cli `champions`/`report` 子命令。

### 不做（留 Plan 4，spec §4.1 daemon.py/publish.py）
schtasks 夜跑守护（`daemon.py`）、experiment DRAFT 闭环（`publish.py`）、多 outer 段轮流、熊市 regime 覆盖（需扩 2020-2024 数据，spec §1.4 真风险三转移 a）。

## 2. 模块架构（spec §4.1 落地）

### 新增
| 模块 | 职责 | spec |
|---|---|---|
| `discovery/pareto.py` | Pareto 非支配前沿（纯函数）+ 收敛判据①③ | §4.1 / §3.5 |
| `discovery/coverage.py` | 参数空间覆盖度度量（判据④，防伪收敛核心） | §3.5 判据④ |
| `discovery/dsr.py` | Deflated Sharpe Ratio（闭式公式，L2 统计裁决） | §3.7 |
| `discovery/search.py` | optuna TPESampler 序贯优化 + Sobol warm start | §4.1 / §7.2 阶段二 |

### 修改
| 模块 | 改动 | spec |
|---|---|---|
| `discovery/constraints.py` | 耦合5（suppression↔decay_tau）语义厘清 | §7.1 耦合5 |
| `discovery/worker.py` | 耦合6（buy_limit<cancel×H/ATR）runtime 裁剪 | §7.1 耦合6 |
| `discovery/runner.py` | 接入 Pareto + 收敛自停（`status` 扩 `"converged"`）+ DSR top-N | §5.2 / §3.5 |
| `discovery/cli.py` | run 接入收敛 + 新增 `champions`/`report` 子命令 | §4.1 cli |
| `discovery/__init__.py` | 导出 Plan 3 API | — |

## 3. 关键设计决策（spec 未完全明确，Plan 3 定；含与 spec 出入标注）

| # | 决策点 | Plan 3 取值 | spec 原文 / 出入 | 理由 |
|---|---|---|---|---|
| 1 | **TPE 目标函数** | inner **calmar** | spec §6.2 v1.1 写"objective=inner ann 几何均"；§3.5 v1.2 主排序改 calmar。**出入**：Plan 3 让 TPE 跟随 v1.2 用 calmar（非 ann） | TPE 优化什么就排序什么；ann 是 `risk_metrics` 复利放大产物（§1.4 实证夏普 15/ann 201% 失真），calmar 贴颈线法回撤风险。父 spec §6.2 的 ann 在 v1.2 已被 calmar 主排序覆盖，Plan 3 建议父 spec 随实施同步 |
| 2 | **覆盖度 ρ 算法** | 网格单元占用率（21 维按候选档分箱→被采单元比例） | spec §3.5 只给"≥ρ=0.8"未给公式 | 简单显式纯函数可单测；离散候选档天然分箱，无需 KDE |
| 3 | **Plan 2 自写 Sobol 去向** | 保留，optuna `study.enqueue_trial` 注入 Sobol 点 warm start TPE | spec §7.2"optuna 一套到位"含 SobolSampler；**出入**：Plan 3 不废弃 Plan 2 已验证的自写 Sobol | 不浪费 Plan 2 投资；§7.2"Sobol 初始覆盖→TPE"两阶段天然契合 warm start |
| 4 | **Pareto 实现** | 自写纯函数 `pareto.py` | spec §4.1 明确自写；§7.2 提 optuna ParetoFrontSampler | 主目标单目标 calmar，Pareto 仅 §3.5 候选筛选补充（v1.2 降级）；optuna 多目标采样器非必需 |
| 5 | **耦合5 处置** | 不裁剪，仅加语义注释/docstring 厘清 | spec §7.1"捆绑调"；Plan 2 实证独立可调 | 代码实证 suppression/decay_tau 独立可调（decay_tau=None 等权时 suppression 仍生效），spec"捆绑"语义已退化为"都可调"；凭空裁剪误杀合法组合 |
| 6 | **耦合6 处置** | `worker._eval_worker` 拿 universe 后 runtime 裁（非法→返回 None 标 failed） | spec §7.1"按当时 H/ATR 剔除"；Plan 2 收窄留 Plan 3 | buy_limit<cancel×H/ATR 依赖 runtime H/ATR（每标的每信号点不同），采样期无法静态判 |

## 4. Task 划分（8 task，TDD，每 task 独立可 review）

| Task | 交付（Files） | 核心接口 | 测试要点 |
|---|---|---|---|
| T1 | `pareto.py` + test | `pareto_frontier(trials) -> list`（非支配）、`frontier_grew(old, new) -> bool`、`converged_k_rounds(history, K=3) -> bool` | 前沿非支配正确性、连续 K 轮不扩张判定、纯函数 |
| T2 | `coverage.py` + test | `grid_coverage(sampled_params) -> float`（网格占用率 ρ）、`coverage_gate(ρ, threshold=0.8) -> bool` | ρ 随采样单调增、判据④前置否决（ρ<阈值时即便前沿不扩张也不许停） |
| T3 | `dsr.py` + test | `deflated_sharpe(sharpe, n_trials, skew, kurt, T) -> float`（闭式） | 多重比较修正（n_trials↑→DSR↓）、非正态修正（skew/kurt）、对 top-N 标显著/运气 |
| T4 | `search.py` + test（**前置：pip install optuna，更新 requirements.txt**） | `tpe_search(seed_points, n_trials, objective_fn) -> list[trial]`（optuna TPESampler + enqueue seed warm start）、`expected_improvement(study) -> float`（判据②） | TPE 集中高 calmar 区、EI 随 trial 递减、可复现、optuna 离散 suggest_categorical |
| T5 | `constraints.py` 耦合5 厘清 + `worker.py` 耦合6 runtime 裁剪 + test | 耦合5：docstring/注释厘清；耦合6：`_eval_worker` 内 runtime H/ATR 判 | 耦合6 非法组合→None（spec §7.1）、耦合5 不误杀 |
| T6 | `runner.py` 接入 + test（monkeypatch） | `run_search` 内：采 TPE→eval→Pareto 更新→收敛判据①②③④→`status="converged"` 自停；DSR 标 top-N | 收敛自停（mock 判据命中）、信息隔离（Pareto/排序只用 inner）、零回归 |
| T7 | `cli.py` + test | run 接入收敛打印；新增 `champions`（DSR+Pareto 报告）、`report`（run 历史）子命令 | 端到端、子命令注册 |
| T8 | slow 集成测试 | 真实 optuna TPE 小 budget 跑批收敛 | 收敛自停、SQLite 落库、断点续跑零回归 |

**依赖顺序**：T1→T2→T3 可并行（独立纯函数）；T4 依赖 optuna 安装；T5 独立；T6 依赖 T1/T2/T3/T4；T7 依赖 T6；T8 依赖全部。

## 5. 与 Plan 2 衔接（不重造）

- **Sobol warm start**：Plan 2 `sampler.sobol_sample` 产初始点 → T4 `search.py` `study.enqueue_trial` 注入 optuna study → TPESampler 从 warm start 序贯。
- **runner status 扩展**：Plan 2 `RunSummary.status="budget_exhausted"` → Plan 3 扩 `"converged"`（判据①②③④任一命中）。`budget_exhausted` 仍是判据③的实现。
- **store 三表复用**：Plan 2 trial 表已有 `inner_metrics`/`outer_metrics` JSON，Plan 3 Pareto/DSR 从 store 读 trial 计算，不改 schema（DSR 结果入 `convergence_note` 或新增列由 T6 定）。
- **信息隔离不变**：Plan 2 `top_inner_calmar` 只用 inner；Plan 3 Pareto/排序/DSR 全只用 inner（spec §6.2），outer 仍仅报告。

## 6. 诚实边界（spec §3.5/§13 评价天花板）

- **DSR 功效天花板**：颈线法信号稀疏（单组几百笔、切折后每折几十~百笔），DSR 置信区间宽。spec §3.7 明示"极端情况 DSR 可能说 top-5 优劣在噪声内不可辨——那就承认'相对最优'在该数据量下不可辨识，而非硬选"。Plan 3 T3 如实报置信区间，不强选（对齐 ADR13）。
- **判据④覆盖度 ρ=0.8 是初定值**：spec §3.6 说"按算力账标定的覆盖所需夜数反推"。Plan 3 取 ρ=0.8 作默认（spec 初定），实际标定留 Plan 4 daemon 跑后回溯。
- **TPE 目标 calmar 偏离 spec §6.2 ann**：见决策1，Plan 3 实施时同步父 spec（或父 spec 留 ann、Plan 3 文档存档偏离理由）。

## 7. Self-Review（写完后自查）

- **Spec 覆盖**：§7.2 TPE（T4）✓、§3.5 收敛判据①②③④（T1/T2/T4/T6）✓、§4.1 pareto.py（T1）✓、§3.7 DSR（T3）✓、§7.1 耦合5/6（T5）✓、§5.2 自停循环（T6）✓。
- **Placeholder 扫描**：无 TBD/TODO；ρ=0.8 是 spec 既定默认（非 placeholder），实际标定留 Plan 4（§6 已标注）。
- **内部一致性**：TPE 目标 calmar（决策1）与 Pareto/排序/DSR 全用 inner calmar 一致；判据④前置否决（T2）与 T6 自停逻辑一致。
- **范围**：8 task 单 plan 可实施（T1-T5 多为独立纯函数，T6 集成，T7/T8 收尾）；schtasks/publish 留 Plan 4（§1 已标注）。
- **歧义**：DSR 结果存储（新列 vs convergence_note）T6 定；覆盖度网格分箱粒度（按候选档 vs 更细）T2 定——均为实现细节，不阻塞 plan 成立。
