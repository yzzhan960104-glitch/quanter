# 风控逻辑废弃删除与整体重构 — Spec 变更预期（DRAFT）

> 状态：DRAFT，仅整理预期改动，未实施。
> 决策来源：`docs/superpowers/plans/2026-08-05-risk-controls-removal-pending.md`
> 已并入：`docs/superpowers/specs/2026-08-05-process-gateway-ssot-final-design.md`（Phase A）
> 核心决策：废弃删除当前风控所有逻辑，待后续链路调通后整体重构。

## 1. 目标与范围

- 删除当前全部风控拦截逻辑（下单挡板、网关/熔断层、引擎流程闸、计划确认/veto、
  启动影子闸、白名单、订单状态机等）。
- 删除前置条件：**交易主链路先调通**（计划 → 挂单 → 成交回报 → 止损/止盈 →
  对账 → 重启恢复），否则删除风控=真金裸奔。
- 链路调通后按新架构整体重构，而不是逐个补丁。

## 2. 现状盘点（要删的东西）

### 2.1 下单挡板（10 关）

| 关 | 位置 | 触发 | 删除后行为 |
|---|---|---|---|
| connection | `trading/compute/risk.py:81` | 网关未连接/锁定 | 待定（见 §6 未决） |
| dry_run | `trading/compute/risk.py:85` | 请求级模拟 | 保留语义？待定 |
| allow_live | `trading/compute/risk.py:89` | `QMT_ALLOW_LIVE_TRADE=false` | 待定 |
| confirm | `trading/compute/risk.py:93` | 二次确认缺失 | 待定 |
| whitelist | `trading/compute/risk.py:97` | 不在白名单 | 删除 |
| lot | `trading/compute/risk.py:101` | 非 100 整手 | 删除（A 股硬约束改由柜台兜底） |
| max_amount | `trading/compute/risk.py:108` | 超单笔金额 | 删除 |
| max_shares | `trading/compute/risk.py:116` | 超单笔股数 | 删除 |
| high/low_limit | `trading/compute/risk.py:123` | 涨停 BUY/跌停 SELL | 删除 |
| session | `trading/compute/risk.py:139` | 非 09:30–11:30/13:00–15:00 | 删除（09:22 挂单恢复） |

调用链：`engine._submit` → `trading_service.submit_order` →
`check_order` → `RiskDecision` → BLOCKED/DRY_RUN 审计。

### 2.2 网关/熔断层（`broker/qmt.py`、`trading/compute/breaker.py`、`trading/io/breaker.py`）

- `is_blocked = _risk_halted or _lock_down`（qmt.py:1116）
- 断线保护：`on_disconnected` → `_lock_down=True`（qmt.py:1281）
- 账号状态熔断（qmt.py:111 的 fatal 状态集合）
- 人工紧急熔断 `emergency_halt`（trading_service.py:396）
- 日内 -3% 熔断：`check_daily_loss_limit`（breaker.py:29）+ post_close 触发
- 锁定态下 submit/cancel/query_asset/positions 全部拒
- health_guard 重连退避与锁保持逻辑

### 2.3 引擎流程闸（`trading/engine.py`）

- pre_open 三段 gate：计划确认 / 网关健康 / 数据就绪（engine.py:2208）
- `_gw_health_gate`（engine.py:2178，pre_open/stop_loss/post_close 共用）
- veto 保护、has_order 幂等、max_wait 过滤（engine.py:897-908）
- `_CriticalHalt` / `_critical_guard` L1 停调度（engine.py:135）
- stop_loss 交易日/盘中/网关闸（engine.py:2915、1042）
- pipeline_then_eod 数据闸与采集失败拦截（orchestrate/pipeline.py:104）
- 动态白名单注入/清理（engine.py:853、post_close）

### 2.4 计划/人审层

- `plan.confirmed` 确认闸（trading_plan.py）
- `confirm_plan` / veto_plan 的 VETOED 保护（trading_plan.py:119）

### 2.5 启动/部署层

- 影子期硬闸 `check_shadow_gate`（`trading/__main__.py:229`，当前阈值 1 天）
- QMT session 单实例锁（single_instance.py）
- 端口 8000 单例（`trading/__main__.py:160`）

### 2.6 白名单

- 静态 `QMT_SYMBOL_WHITELIST`（当前 4 只 ETF）
- 动态白名单机制（dynamic_whitelist.py）

### 2.7 订单状态机与幂等

- `OrderStateMachine` 非法迁移拒绝（order_state.py）
- `has_order` 死态重挂许可（state_store.py:774）
- job_ledger running/done 防双跑（调度闸，是否保留待定）

### 2.8 配置项（`.env` / `.env.example`）

`QMT_ALLOW_LIVE_TRADE`、`QMT_ENFORCE_SESSION`、`QMT_ORDER_MAX_AMOUNT`、
`QMT_ORDER_MAX_SHARES`、`QMT_SYMBOL_WHITELIST`、`TRADE_SHADOW_MIN_DAYS`、
`CIRCUIT_DAILY_LOSS_LIMIT`、`AUTO_TRADE_MODE`、`AUTO_CONFIRM_PLAN`。

### 2.9 P0–P2 调度鲁棒性改造点对照

| 编号 | 问题 | 本 spec 是否覆盖 |
|---|---|---|
| P0-1 | session 关与 09:22 调度冲突（今天废单根因） | ✅ §2.1 删除 session 关；未决 Q2 确认 09:22 集合竞价挂单恢复 |
| P0-2 | 台账 `done` 掩盖 0 成交，C-8 窗口不重试 | ❌ 需新增：pre_open 返回 submitted/rejected；台账增加 partial/failed 语义；窗口内按 has_order 幂等重试 |
| P0-3 | 常驻进程跑旧代码，代码更新后未重启不生效 | ❌ 需新增：启动日志/健康检查暴露代码版本与进程启动时间；代码变更后强制重启告警 |
| P1-1 | schtasks 实际是 LogonTrigger 非 ONSTART，且无重启策略 | ❌ 需新增：重新注册 ONSTART + RestartOnFailure，并验证 XML |
| P1-2 | 大量 dry_run 启动噪音，可能以 dry_run 抢占生产 | ❌ 需新增：开发实例独立日志/端口；生产启动 fail-closed（AUTO_TRADE_MODE 必须显式 live） |
| P2-1 | 时段判定三处重复定义且用本机时间 | 部分：§6 重构收口；需明确 calendar / trading_service / config.market 合一到统一 clock |
| P2-2 | 单笔失败对驾驶舱不可见（台账 message 为空） | ❌ 需新增：finish_run message 写 submitted/rejected/拒因 |
| P2-3 | M4 漏挂告警不可审计（fire_and_forget 成功无日志） | ❌ 需新增：notifier 成功/失败落审计日志 |

## 3. 测试改造清单

- `tests/test_risk_shield.py`：10 关用例删除或改语义
- `tests/trading/test_engine_pre_open_gate.py`：三段 gate 用例
- `tests/trading/test_veto_plan_db.py`：veto 保护用例
- `tests/trading/test_qmt_health_guard.py`：锁/重连用例
- `tests/trading/test_engine.py`：stop_loss 时段/交易日闸用例
- `tests/trading/test_l2_aggregated_critical.py`：漏挂告警用例
- `tests/trading/test_catchup.py` / `test_job_ledger.py`：P0-2 台账语义与重试用例
- 新增：P0-3 版本/重启守卫、P1-2 生产 fail-closed 启动用例
- `tests/e2e_long_cycle/*`：熔断/风控相关编排用例
- `tests/server/*`：BLOCKED/DRY_RUN 审计用例
- 新增：链路调通验收测试（见 §4）

## 4. 分阶段预期

### Phase 0 — 冻结与记录（当前）
- 决策记录已落：`docs/superpowers/plans/2026-08-05-risk-controls-removal-pending.md`
- 本 spec DRAFT 作为后续实施蓝图

### Phase 1 — 交易主链路调通（前置，必须先做）
验收标准（可测）：
- pre_open 能成功挂单（含 09:22 集合竞价，session 关不再误拦）
- 成交回报回流 order/fill/position，无幽灵单、无重复挂
- stop_loss/止盈/撤单/对账/重启补跑全链路 live 跑通
- 审计事件（SSoT）完整可查
- P0-2：台账不再用 done 掩盖 0 成交，失败可在窗口内自动重试
- P0-3：代码版本与进程启动时间可查，更新后未重启能告警
- P1-1：QuanterServer 为 ONSTART 且有 RestartOnFailure
- P1-2：生产启动 fail-closed，dry_run 实例不污染生产日志/端口
- P2-2/P2-3：单笔拒因进台账 message，告警成功/失败可审计

### Phase 2 — 风控废弃删除
建议顺序（每步独立可回滚）：
1. 配置层：删 .env 风控项 + .env.example 同步
2. 挡板层：`check_order` 简化/删除，`submit_order` 只保留连接与审计
3. 引擎闸：pre_open gate / stop_loss 闸 / pipeline 闸逐个移除
4. 熔断层：emergency_halt / 日内熔断 / 断线锁删除或降级
5. 启动层：影子闸 / 单实例锁 / 端口单例评估
6. 状态机/幂等：与重构合并评估

### Phase 3 — 整体重构
- 单一风控入口（新接口），配置与判定分离
- 静态体检前移：金额/股数/白名单/时段等静态可判项在 `eod_plan` 生成期检查
- 运行态只保留必要闸（连接、幂等、审计）
- 可观测：拦截/放行全量审计
- P2-1：时段/交易日判定收口到单一函数 + 统一 clock，删除重复定义

## 5. 文档与配置联动

- `README.md` 风控章节重写
- `docs/superpowers/specs/*` 历史 spec 标注废弃或归档
- `.env.example` 同步
- 前端/Cockpit 状态展示（lock/vetoed_by_risk 等）同步调整

## 6. 未决问题（spec 评审时需回答）

1. “所有风控”是否包含断线保护、幂等、审计？若全删，主链路的防重复挂/防幽灵单靠什么？
2. 删除后 09:22 集合竞价挂单是否恢复为唯一目标（即 session 问题自然消失）？
3. 白名单删除后，前端手动下单路径的标的约束是否也放开？
4. 影子闸删除后，LIVE 启动是否无条件放行？
5. 日内 -3% 熔断删除后，异常回撤靠什么兜底（人工盯盘？）？
6. “链路调通”的验收标准由谁定、何时验收？
7. 删除过程中是否需要 dry_run 过渡期？

## 7. 风险与回滚

- 最大风险：链路未通就删风控 = 真金无保护运行。
- 每阶段独立提交、独立回滚；删除前置入测试改造，禁止一次性大删。
