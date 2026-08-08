> 最近复核：2026-08-08 · 维护者：wayfinder-session ·
> 权威归宿：本文件是架构文档集的**索引 + 单一归宿映射表**（[T0](../../plans/wayfinder/T0.md) 决策）。
> 图约定：Mermaid（[T0 Decisions](../../plans/wayfinder/T0.md) (b)）。**活文档**——随代码演进，改动时改文件头「最近复核」。

# quanter 架构全景（Phase 0 地基）

本目录是《quanter 架构演进蓝图 + 路线图》的**现状梳理**产物（wayfinder [T0](../../plans/wayfinder/T0.md)），作为 T1（engine 拆分）/ T2（适配层）/ T3（三维扩展）及横向工单（T6 / T9-T13）的**公共地基**。

> **形式**：topic-flat + Mermaid（不采 C4 模型；#1 仅借 C4-L1 idiom）。
> **颗粒度**：8 全景视图 + engine 当前态深剖（[T0 Decisions](../../plans/wayfinder/T0.md)）。

## 8 视图索引

| # | 视图 | 文件 | 图种 | 单一归宿权威 |
|---|---|---|---|---|
| 1 | 系统上下文 | [01-system-context.md](01-system-context.md) | `flowchart`（借 C4-L1） | 外部系统边界 |
| 2 | 模块边界 + 依赖 | [02-module-dependencies.md](02-module-dependencies.md) | `flowchart` | **模块/包/依赖** |
| 3 | 核心数据流 | [03-data-flow.md](03-data-flow.md) _(待产出)_ | `sequenceDiagram`+`flowchart` | **数据路径** |
| 4 | 进程/调度拓扑 | [04-process-topology.md](04-process-topology.md) _(待产出)_ | `flowchart` | 进程/调度结构 |
| 5 | 状态机 | [05-state-machines.md](05-state-machines.md) _(待产出)_ | `stateDiagram-v2` | 状态迁移 |
| 6 | 技术债/缺口 | [06-tech-debt.md](06-tech-debt.md) _(待产出)_ | `flowchart`（债子集+色块） | **债务判定** |
| 7 | SSoT 数据层 | [07-ssot-data-layer.md](07-ssot-data-layer.md) _(待产出)_ | `classDiagram`（α 补集） | 数据模型图 |
| 8 | 控制/时间流 | [08-control-time-flow.md](08-control-time-flow.md) _(待产出)_ | `flowchart`（job 切片） | 时间窗/时钟语义 |
| — | engine 深剖 | [deep-dives/engine-current-state.md](deep-dives/engine-current-state.md) _(待产出)_ | 多图 | engine 内部结构 |
| — | 演进路线图 | [roadmap.md](roadmap.md) _(待产出)_ | — | 目标态 / Phase 编排 |

## 单一归宿映射表（[T0 乙原则](../../plans/wayfinder/T0.md)——每事实只在一处权威定义，他处引用不重抄）

| 事实类型 | 权威归宿 | 他视图引用方式 |
|---|---|---|
| 外部系统边界与集成入口 | **#1** | #4 引用外部触发源（schtasks） |
| 模块/包清单 + 包间依赖边 | **#2** | #6 提模块时链 #2；engine 内部链深剖 |
| 数据路径（采集→lake→信号→计划→订单→成交→对账→持仓） | **#3** | #7 链 #3 |
| 进程模型 / 调度结构 | **#4** | #8 链 #4 |
| 订单/计划/持仓 状态迁移 | **#5** | #3 / #6 引用状态枚举 |
| 技术债 / 痛点 / god module 判定 | **#6** | #2 只画结构、不判债 |
| 数据模型 ER（9 域关系） | **#7**（图） | #3 数据节点链 #7 |
| 数据 SSoT 规则 / 域表 / 护栏 / 巡检 | [`docs/data-source-of-truth.md`](../data-source-of-truth.md) | #7 prose 链此（α：图补 prose 缺） |
| job 时间窗 / 时钟语义（C6 统一） | **#8** | #4 链 #8 |
| 颈线法策略算法 | [`docs/neckline-method.md`](../neckline-method.md) | #3 信号阶段链此 |
| 风控 / 挡板 / 熔断规则 | [`docs/guardrails.md`](../guardrails.md) | #4 / #5 链此 |

## 分层模型（自包间 import 扫描涌现，详见 #2）

```
L0 基础     : infra(884) · config(1007)         ← 最多被依赖（infra 8 包入边 = 真地基）
L1 数据     : data(6400)                        ← 依赖 config + infra
L2 策略契约 : strategies(2000)                  ← Strategy Protocol，backtest/discovery/trading 共用
L3 执行内核 : trading(10620) · broker(2057)     ← trading 最大 fan-out 中枢；含 engine.py 3437（深剖见 deep-dives）
L4 分析     : backtest(4134) · discovery(2942) · experiment(569) · compute_unit(771)
L5 接口运维 : presentation(3049) · broadcast(1607) · ops(1746)
合计 ≈ 37.7k 行 · 224 .py 文件
```

## 维护协议

1. **活文档**：架构随代码演进。改代码影响某视图时，**同一改动**内更新该视图 + 改文件头「最近复核」日期。**不开 v1/v2 子目录**（git 即版本）。
2. **单一归宿**：新事实归入上表对应权威视图，**禁止在他视图重抄**——他视图只链接（`见 #2` / `[data-source-of-truth.md]`）。
3. **新增依赖边**：包间新 import → 重跑 [02](02-module-dependencies.md) 文末扫描脚本核对。
4. **过时即删**：`data_pool.md` / `caisen-methodology-summary.md` 已判**丙删**（[T0](../../plans/wayfinder/T0.md) 决策，待执行）。

## 相关文档（不在本目录，被本集引用）

- [`docs/data-source-of-truth.md`](../data-source-of-truth.md) — 数据 SSoT 权威（9 域 prose + 护栏 + 巡检）
- [`docs/neckline-method.md`](../neckline-method.md) — 颈线法策略（当前唯一策略）
- [`docs/guardrails.md`](../guardrails.md) — 风控 / 挡板 / 熔断
- [`docs/superpowers/`](../superpowers/) — 历史 spec / plan 归档（深历史，不作参考）
