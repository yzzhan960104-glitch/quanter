> 最近复核：2026-08-08 · 维护者：wayfinder-session ·
> 权威归宿：**演进目标态 / Phase 编排**（living）。现状见 #1-#8；债务见 [#6](06-tech-debt.md)。**本文随 T1-T3 推进填充**——当前是骨架。

# 演进路线图（living）

《quanter 架构演进蓝图》的分阶段路线图。**演进优先于 live**，但设「可上 live 判据」里程碑防无限演进。

## Phase 编排

### Phase 0 — 现状架构全景梳理 ✅（本视图集，[T0](../../plans/wayfinder/T0.md)）
- 8 全景视图（#1-#8）✅
- engine 当前态深剖 → **毕业 [T0.1](../../plans/wayfinder/T0.1.md)**（触发式，超载毕业）
- 过时文档清理 ✅（丙删 data_pool / caisen-summary）

### Phase 1 — `engine.py` 模块化拆分（[T1](../../plans/wayfinder/T1.md) · 阻塞于 T0.1）
- god module 3437 行 → 「可编排内核 + 职责分离」
- 缝合点：trading↔broker / ↔data / ↔presentation 三处双向耦合（[#2](02-module-dependencies.md)）
- 红线：状态机语义（[#5](05-state-machines.md)）不变形；数据路径（[#3](03-data-flow.md)）不断
- _待 T1 session 细化目标态_

### Phase 2 — 可插拔适配层（[T2](../../plans/wayfinder/T2.md) · 阻塞于 T1）
- broker 业务层重构（qmt.py 1540 堆补丁 → 适配层契约）
- 契约核心切点：trading↔broker 回调写 DB（[#2](02-module-dependencies.md)）
- _待 T2 session 细化_

### Phase 3 — 三维扩展（[T3](../../plans/wayfinder/T3.md) + 待定 · 阻塞于 T2）
- 多策略 × 多资产（港/美/期）× 多账户
- 落地顺序待定（多策略/多资产/多账户谁先——MAP Not-yet-specified）
- compute_unit 在三维扩展下的角色待定

## 横向并行（不阻塞主脊柱，并行推进）

| 工作流 | 工单 | 状态 |
|---|---|---|
| 连接韧性（毕业自 T4） | [T9](../../plans/wayfinder/T9.md) 高 / [T10](../../plans/wayfinder/T10.md) 中 / [T11](../../plans/wayfinder/T11.md) 低 | 待领取 |
| data 完整性根治 | [T13](../../plans/wayfinder/T13.md)（生产 gate + 自动补采 + 写入守卫） | 待领取（环境/活债 T14/T15 已根治） |
| state_store SSoT 演进 | [T6](../../plans/wayfinder/T6.md) | 待领取 |

## 横切决策

- [T7 验证策略](../../plans/wayfinder/T7.md) — 演进期如何验证不回归
- [T8 依赖态度](../../plans/wayfinder/T8.md) — 引入/收敛依赖的取舍

## 里程碑：可上 live 判据（待量化 — MAP Not-yet-specified）

四大痛点收口 + 多策略/多账户隔离经模拟盘验证。具体量化标准待 T6 + T9-T13 横向工单成形后落地。
