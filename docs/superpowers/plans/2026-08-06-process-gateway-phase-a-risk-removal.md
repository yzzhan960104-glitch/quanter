# Phase A 风控批量废弃删除 实施计划（process-gateway-phase-a-risk-removal）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **状态：PENDING——执行前必须先完成 §0 裁定门**（spec §2.4 / risk-controls-removal-design §6 的未决问题）。

**Goal:** 按研究员决策（Annotation 1）废弃删除当前风控拦截逻辑，打通「计划 → 挂单（含集合竞价）→ 成交回报 → 止损/止盈 → 对账 → 重启恢复」全自动链路；删除前置 = 链路调通验收（process-gateway spec §7.1）已过。

**Architecture:** 分层删除（配置层 → 挡板层 → 引擎闸 → 熔断层 → 启动层 → 状态机），每层独立 commit 可回滚；「链路正确性原语」（单实例锁、端口单例、幂等、审计、断线保护）默认保留，是否一并删由裁定门 D1 决定。

**Tech Stack:** Python 3.10（`.venv310`）、pytest、`trading/compute/risk.py`、`trading/engine.py`、`trading_service.py`、`broker/qmt.py`、`.env`。

## Global Constraints

- 全中文注释；定位用符号名；TDD；`QUANTER_TESTING=1` 测试隔离（B3 已落地）。
- **红线 1（先通后删）**：§7.1 链路调通验收全过后才允许批量删除；未过验收只保留 A1 的 session 关最小修复（已落地 09:15）。
- **红线 2（默认保留原语）**：单实例锁 / 端口单例 / 成交·订单幂等 / 审计事件 / 断线保护默认不删；删除需裁定门 D1 显式通过。
- **红线 3（可回滚）**：每 task 独立 commit；禁止一次性大删。

## 0. 裁定门（执行前必须逐项裁定并回写本 plan）

| # | 问题（spec §2.4 / DRAFT §6） | 默认建议 | 影响 |
|---|---|---|---|
| D1 | 「所有风控」是否含断线保护、幂等、审计？ | 不含（保留） | 决定 T4/T6 范围 |
| D2 | session 关删除后 09:22 集合竞价挂单是否唯一目标？ | 是（A1 已修 09:15，本 plan 删除 session 关或保留 A1 二选一） | 决定 T2 是否删 session 关 |
| D3 | 白名单删除后，前端手动下单路径是否也放开？ | 放开（同一 check_order 路径） | 决定 T2 测试范围 |
| D4 | 影子闸删除后 LIVE 是否无条件放行？ | 保留影子闸（降级为 WARN） | 决定 T5 |
| D5 | 日内 -3% 熔断删除后回撤靠什么兜底？ | 保留 emergency_halt 人工刹车 + 告警 | 决定 T4 |
| D6 | 链路调通验收由谁验收、何时？ | 08-06 09:22 实测 + audit_ssot 全绿 | 决定启动时间 |
| D7 | 删除过程是否需要 dry_run 过渡期？ | 需要（先 dry_run 一个交易日） | 决定 T1 顺序 |

## File Structure

| 文件 | 动作 |
|---|---|
| `trading/compute/risk.py` | T2 删 10 关（或按 D2 保留 session 关） |
| `presentation/server/services/trading_service.py` | T2 submit_order 只留连接/审计 |
| `trading/engine.py` | T3 删 pre_open gate/_gw_health_gate/veto/max_wait；T4 删熔断触发（按 D5） |
| `trading/compute/breaker.py` / `trading/io/breaker.py` | T4 删日内熔断（按 D5） |
| `trading/__main__.py` | T5 影子闸降级/删除（按 D4） |
| `trading/order_state.py` / `state_store.py` | T6 状态机/幂等评估（按 D1） |
| `.env` / `.env.example` | T1 删配置项 |
| `tests/test_risk_shield.py` 等 | T7 同步改造 |
| `docs/data-source-of-truth.md` / README | T8 文档同步 |

---

## Task A-1: 配置层（先删 .env，代码最后删）

- [ ] Step 1: 写失败测试——`.env.example` 不再含待删键
- [ ] Step 2: 实现——按 D2-D5 裁定结果从 `.env`/`.env.example` 删除或注释对应键
- [ ] Step 3: 验证——`rg "QMT_ORDER_MAX_AMOUNT|QMT_SYMBOL_WHITELIST|CIRCUIT_DAILY_LOSS_LIMIT" .env .env.example`
- [ ] Step 4: Commit `chore(env): A-1 风控配置项退役（待代码层 T2 收口）`

## Task A-2: 下单挡板 10 关

- [ ] Step 1: 写失败测试——`check_order` 对 `in_session=False/whitelist 外/超金额` 不再 blocked（按裁定保留项除外）
- [ ] Step 2: 实现——`check_order` 简化；`submit_order` 只保留连接闸 + trade_event 审计；删 `RiskDecision` 分支
- [ ] Step 3: 验证——`pytest tests/test_risk_shield.py tests/test_trading_service.py`
- [ ] Step 4: Commit `feat(risk): A-2 挡板 10 关删除（按裁定门保留项除外）`

## Task A-3: 引擎流程闸

- [ ] Step 1: 写失败测试——pre_open gate 三项不再拦截（`skipped` 语义保留）
- [ ] Step 2: 实现——`_pre_open_gate` 直通或删除；`_gw_health_gate` 降级为日志；veto/max_wait 按裁定
- [ ] Step 3: 验证——`pytest tests/trading/test_engine_pre_open_gate.py tests/trading/test_pre_open_ledger_semantics.py`
- [ ] Step 4: Commit `feat(engine): A-3 引擎流程闸删除（台账 failed 语义保留）`

## Task A-4: 熔断层（按 D1/D5）

- [ ] Step 1: 写失败测试——`check_daily_loss_limit` 不再触发 halt（或保留 emergency_halt）
- [ ] Step 2: 实现——删除 `_CriticalHalt` 之外的熔断触发；保留人工 `emergency_halt` 端点（默认）
- [ ] Step 3: 验证——`pytest tests/trading/test_engine.py -k circuit tests/trading/test_circuit_breaker.py`
- [ ] Step 4: Commit `feat(risk): A-4 日内熔断删除/降级（D5 裁定）`

## Task A-5: 启动层（按 D4）

- [ ] Step 1: 写失败测试——`check_shadow_gate` 行为按裁定
- [ ] Step 2: 实现——影子闸降级 WARN 或删除；`QUANTER_REQUIRE_LIVE` 保留
- [ ] Step 3: 验证——`pytest tests/trading/test_main_shadow_gate.py tests/trading/test_main.py`
- [ ] Step 4: Commit `feat(main): A-5 影子闸按 D4 裁定落地`

## Task A-6: 状态机/幂等评估（按 D1）

- [ ] Step 1: 写评估测试——删除非法迁移拒绝后 `has_order` 幂等是否仍成立
- [ ] Step 2: 实现——D1 通过才删；默认保留
- [ ] Step 3: Commit `chore(risk): A-6 状态机/幂等评估结论回写`

## Task A-7: 测试与文档收口

- [ ] Step 1: 全量 `pytest tests/ -q`（默认排除 slow/e2e_long/server_lifecycle）
- [ ] Step 2: 更新 `docs/data-source-of-truth.md`、README 风控章节、`docs/superpowers/specs/2026-08-05-risk-controls-removal-design.md` 状态
- [ ] Step 3: Commit `docs(risk): A-7 风控删除收口`

---

## 验收（§7.1 + A 特有）

1. 09:22 集合竞价挂单成功（A1 + D2）。
2. 台账 `failed` 语义在 C-8 窗口可重试（A2 已落地，不回归）。
3. audit_ssot 全绿（含进程拓扑三项）。
4. dry_run 过渡期（D7）一个交易日无异常后才允许 live 裸跑。

## 回滚

每个 task 独立 revert；`.env` 改动先备份；T2 删除前确认 T1 的配置键已无代码引用。
