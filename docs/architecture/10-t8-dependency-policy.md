# ADR-10 · T8 架构演进的依赖态度边界

> 决策门 DG-2 · status: Accepted（推荐口径，待用户终签）· 2026-08-12
> 横切护栏：约束所有重构工单的依赖引入。源自 [T8](../../plans/wayfinder/T8.md) + 综合治理总纲 spec §2.2。

## Context（为什么需要这个决策）

三维扩展（多策略/多资产/多账户，T2/T3）可能诱发「引入消息总线 / IPC 框架 / DI 容器」冲动。CLAUDE.md 强约束：拒绝重型黑盒框架、显式至上。**必须先定边界**，避免各工单各自引入造成依赖爆炸、违背极简哲学。

## Decision（严守 Karpathy 极简）

**基线立场**：纯标准库 + `Protocol`/`ABC`/`dataclass`。

**适配层 T2 纯标准库可实现**（论证）：
- 插件发现 → `importlib`（标准库）/ `importlib.metadata.entry_points`；
- 配置组合 → `dataclass` + dict；
- 生命周期钩子 → `Protocol` methods（`pre_open` / `on_signal` / `on_fill` / `eod`）。
- 不需要 DI 框架 / 事件总线 / 插件黑盒。

**多账户硬隔离（T3 若选硬隔离）** → 标准库 `multiprocessing`，**不引入 IPC 框架**（zerorq/mq 等）。

### 绝不引入清单（一律拒）

| 类别 | 例 | 拒绝理由 |
|---|---|---|
| 重型量化黑盒框架 | qlib / backtrader / vnpy 全家桶 | 魔法不可审计，锁版本成本高，与现有颈线法/backtest 体系冲突 |
| 魔法 DI 容器 | dependency-injector / inject | 隐式装配，调试困难，纯标准库 Protocol 注入足够 |
| 隐式事件总线 | blinker / pydispatch / celery | 隐式控制流，调用链不可追；现有回调注入 + apscheduler 显式调度足够 |
| ORM 框架 | sqlalchemy ORM | state_store 用裸 sqlite3 显式 SQL（可审计、零魔法），ORM 增间接层 |

### 可引入清单

**暂空**。审批口径：每提一个候选，必须回答：
1. 纯标准库为什么不行？（具体痛点，非「方便」）
2. 引入后的可审计性 / 调试性 / 锁版本成本？
3. 是否有更轻的显式替代？

三问都过 → 记一条 ADR 增项 + 用户签，才引入。

## 对抗性推演（拷问）

- **「多账户硬隔离用 multiprocessing 性能差」** → 隔离是正确性需求（资金安全），非性能；A 股单账户低频，进程间开销可接受。若未来需高频，再议（届时重开 ADR）。
- **「importlib 插件发现不如框架方便」** → 方便不是理由；显式 registry（dict + 装饰器）比 importlib 扫包更可审计，适配层数量有限（4 个），手写注册零负担。
- **「Protocol 运行时无类型检查」** → 静态 mypy/IDE 检查足够；运行时契约由 L2 conformance 测试兜（ADR-09）。

## Consequences

- 适配层 / 多账户实现工作量略增（手写 vs 框架）——但可审计、零锁版本、零魔法。
- 拒引入清单是硬约束——任何工单试图引入需先过三问 + ADR 增项。
- 依赖面保持极小（现状 ~纯标准库 + tushare/pandas/fastapi/akshare 业务依赖），演进不增架构依赖。

## 相关

- 综合治理总纲 spec §2.2 / §0.2 硬约束
- [T8 工单](../../plans/wayfinder/T8.md) / [MAP 演进护栏](../../plans/wayfinder/MAP.md)
