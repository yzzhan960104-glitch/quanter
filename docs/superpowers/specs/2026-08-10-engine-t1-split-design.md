> 日期：2026-08-10
> 状态：待评审
> 范围：`trading/engine.py`（3437 行 god module）模块化拆分 —— wayfinder [T1](../../../plans/wayfinder/T1.md)
> 依赖：[T0.1](../../../plans/wayfinder/T0.1.md)（engine 内部结构深剖，已毕业）→ 解封 T1
> 解封：[T2](../../../plans/wayfinder/T2.md)（broker 适配层）阻塞于本工单
> 权威归宿：engine T1 拆分的**单一设计真相源**。engine 内部结构（集群/缝合点/调用图）引用 [engine-current-state](../../architecture/deep-dives/engine-current-state.md)，不重抄；状态机语义归 [#5](../../architecture/05-state-machines.md)；技术债判定归 [#6](../../architecture/06-tech-debt.md)。

# `engine.py` 模块化拆分设计（T1）

## 1. 背景与动机

### 1.1 痛点
`trading/engine.py` 3437 行 = god module（trading 包 10620 行的 32%），承载「一日内四阶段交易动作」（数据加载→信号→计划→挂单→止损→止盈→对账→成交回报）与「进程生命周期」（bootstrap / scheduler / health_guard / broadcast）共 **10 个责任集群**于一文件。模块级函数（`pre_open`/`post_close`/`stop_loss_monitor`）与类方法（`_pre_open`/`_post_close`/`_stoploss`）职责镜像、经 `_ACTIVE_ENGINE` 单例桥双向互调，任一职责的修改都在同文件内散弹，且模块级全局依赖（`_state_store`/`_position_book`/`lake`）致其难以单元测试。

### 1.2 工单与依据
- [T1](../../../plans/wayfinder/T1.md) Question：选定 engine.py 的**主切分维度** + 给出目标模块树（候选：按职责 / 按生命周期 / 按领域）。
- [T0.1 深剖](../../architecture/deep-dives/engine-current-state.md) 已归纳 10 集群 + 4 缝合点 + 目标态草图 + 拆分顺序红线，**本 spec 据此细化执行设计**，不重复深剖的符号清单。
- grilling 共识 #6：渐进式分层解耦 = 先模块化拆 engine（A = T1）→ 再抽可插拔适配层（B = T2）。**本工单是「先模块化」那一步**。

### 1.3 决策（本 session brainstorming 已定）
| 决策 | 选定 | 理由 |
|---|---|---|
| 主切分维度 | **职责集群为主 + `phases/` 按生命周期阶段** | 契合 10 集群的物理内聚（基础设施/数据/状态/计划按职责；pre_open/stop_loss/post_close/exit 按一日内阶段），深剖 §5 推荐 |
| 拆分范围 | **全拆到 §5 目标态**（8 外迁文件） | 一次到位，scope 完整覆盖 god module |
| 单例桥消除 | **EnginePorts 窄接口**（`pre_open(ports, date)`） | phases 不依赖 `TradingEngine` 类 → 无循环 import、真正解耦、为 T2 适配层铺立足点 |
| Ports 边界 | **窄 Ports**（仅 engine 实例特有依赖） | `state_store`/`lake`/`gateway` 等项目级单例保持模块级访问，不越界 [T6](../../../plans/wayfinder/T6.md)（state_store SSoT 演进） |

## 2. 目标模块树

engine.py 3437 行 → **~800 行**（仅集群 J 生命周期/调度 + 集群 C 网关/gate），外迁 **8 个文件**：

| 外迁模块 | 集群 | ~行 | 职责 | 原符号（深剖 §1.1） |
|---|---|---|---|---|
| `trading/critical.py` | A | 201 | L1 致命停调度语义 + 模式/配置读口 | `_alert_critical`/`_CriticalHalt`/`_critical_guard` + 类方法 `_halt`/`_guard_skip_rounds` |
| `trading/data_ctx.py` | B | 201 | 从 lake 读标的宇宙/日线/计划/完整性上下文 | 6 个 `_load_*` + `_resolve_cooldown_days`/`_resolve_id_window` + `_plan_data_keys` |
| `trading/eod_plan.py` | D | 361 | 颈线法信号 → 计划参数 + trade_event SIGNAL/CONFIRMED 落 DB | `eod_plan` + `_sanity_check_date_alignment` |
| `trading/order_state.py` | I | 325 | broker 订单回调三分支 + 订单状态推进（缝合点 #2） | `_handle_order_update` + `_order_direction`/`_advance_order_state_from_status` |
| `trading/phases/pre_open.py` | E | 389 | 盘前：确认闸→撤昨→熔断基线→平超期→挂单 | `pre_open`（job_ledger 包裹）+ `_pre_open_impl` |
| `trading/phases/stop_loss.py` | F | 695 | 盘中海龟移动止损（grace/step/floor）+ 超期平仓 | `stop_loss_monitor` + `_scan_expired_positions`/`_close_expired_positions` |
| `trading/phases/post_close.py` | G | 344 | 盘后熔断 -3% + 持仓快照 + TP 对账 + 清白名单 | `post_close` + `_seq_for_real_oid`/`_order_state_to_db` |
| `trading/phases/exit.py` | H | 119 | 买单成交 → 限价止盈卖单（has_order(TP1) 幂等） | `place_take_profit` |

**engine.py 保留**：
- 集群 J（生命周期/调度/广播，~515 行）：`__init__`/`bootstrap`/`_health_guard`/`_broadcast_positions_pnl`/`start`/`shutdown` + 5 job wrapper（`_pre_open`/`_stoploss`/`_post_close`/`_pipeline_then_eod`/`_eod`）
- 集群 C（网关/账户/提交/gate，~144 行）：`get_gateway`/`_submit`/`_gw_health_gate`/`_pre_open_gate`/`_resolve_account_id`（深剖 §2.C 裁定：gate 随生命周期留 engine）

> **H 歧义裁定**：深剖说「H 可并入 F 或独立」。本 spec 选**独立 `exit.py`** —— F（695 行）已是最大集群，并入将进一步膨胀；止盈与止损虽同属离场但触发链路独立（止盈=买单成交触发，止损=持仓巡检触发），独立文件更清晰。

## 3. EnginePorts 窄接口（消除 `_ACTIVE_ENGINE` 单例桥）

### 3.1 缝合点 #1 现状（深剖 §3.1）
模块级函数经 `_ACTIVE_ENGINE`（L201 定义 / L2144 赋值）反向访问类实例，3 处使用点：
- L740-741：`_pre_open_impl` 读 → 调 `_pre_open_gate`
- L893-894：`_pre_open_impl` 注入标的到 `self._dynamic_whitelist`
- L1843-1844：`post_close` 清空 `self._dynamic_whitelist`

### 3.2 目标接口
```python
# trading/ports.py（独立文件 —— phases/ 与 engine 共同依赖，不寄生 critical.py，
# 避免 phases → critical 的无谓耦合）
from dataclasses import dataclass
from typing import Callable, Awaitable

@dataclass
class EnginePorts:
    """phases 外迁函数的窄依赖接口 —— 仅 engine 实例特有的、原经 _ACTIVE_ENGINE
    桥接的依赖。项目级单例（state_store/lake/gateway）保持模块级访问，phases 直接
    import（不越界 T6）。"""
    # gate=原 _pre_open_gate：盘前三段闸（plan-confirmed→gateway-health→data-ready），
    # 任一未绿早返 skip；返回 gate 判定结果（具体类型实现时对齐 _pre_open_gate 现签名）。
    gate: Callable[[str, object], Awaitable[object]]
    whitelist_add: Callable[[list[str]], None]          # 注入标的到动态白名单
    whitelist_clear: Callable[[], None]                 # post_close 清空白名单
```

### 3.3 外迁函数签名
```python
# trading/phases/pre_open.py
async def pre_open(ports: EnginePorts, date: str) -> None: ...
async def _pre_open_impl(ports: EnginePorts, date: str) -> None: ...

# trading/phases/post_close.py
async def post_close(ports: EnginePorts, date: str) -> None: ...
```

### 3.4 装配点
`TradingEngine.__init__` 构造 `self._ports = EnginePorts(gate=self._pre_open_gate, ...)`；5 job wrapper 内调 `phases.pre_open(self._ports, date)`。

### 3.5 halt 不经 Ports
`_halt`（停调度）由 `critical._critical_guard` 装饰器在 job 入口捕获 `_CriticalHalt` 时调用，phases 内部只 `raise _CriticalHalt`（不直接调 halt），故 halt 留在 critical.py，不进 Ports。

## 4. 拆分顺序（10 step · 每步独立回归门）

**红线**：即便 scope 是全拆，执行仍按深剖缝合点顺序渐进，**每步带回归门，任一门红即停不累积**。

| Step | 动作 | 回归门（tests/trading + e2e） |
|---|---|---|
| **1** | 建 `EnginePorts` + 消除 `_ACTIVE_ENGINE` 5 处使用点（gate/whitelist 改经 ports） | **全量** tests/trading + tests/e2e_long_cycle |
| **2** | 抽 `critical.py`（集群 A · 最独立） | test_critical_guard + test_pre_open_l1_halt + test_stop_loss_l1_halt |
| **3** | 抽 `order_state.py`（集群 I · 缝合点 #2） | test_engine_order_update_handler + test_fill_db_contract |
| **4** | 抽 `data_ctx.py`（集群 B） | test_data_ready + test_engine_eod_injection |
| **5** | 抽 `eod_plan.py`（集群 D） | test_engine_eod_injection + test_pipeline_then_eod + test_veto_plan_db |
| **6** | 抽 `phases/pre_open.py`（集群 E） | test_pre_open_ledger* + test_engine_pre_open_gate + test_e2e_trading_flow |
| **7** | 抽 `phases/stop_loss.py`（集群 F · 最大 695 行） | test_stop_loss* + test_engine_stoploss_inject + test_stop_loss_monitor_decide_exit |
| **8** | 抽 `phases/post_close.py`（集群 G） | test_post_close_reconcile + test_circuit_breaker + test_stoploss_post_close_gate |
| **9** | 抽 `phases/exit.py`（集群 H） | test_engine_order_update_handler（止盈触发链）+ 相关 |
| **10** | **最终 e2e 长周期回归**（行为等价终验） | tests/e2e_long_cycle/test_e2e_long_cycle.py 全程双跑对比 |

每步 = 移动符号 + engine.py re-export（§5.1）+ 跑该步回归门。

## 5. 行为等价红线

### 5.1 公共 API 兼容（外部零改动）
engine.py 原 export 的符号（`pre_open`/`post_close`/`stop_loss_monitor`/`eod_plan`/`get_gateway` 等），通过 engine.py 顶部 re-export 保持外部调用不变：
```python
# trading/engine.py 顶部
from trading.critical import _alert_critical, _CriticalHalt, _critical_guard  # noqa: F401
from trading.data_ctx import _load_universe, _load_df_upto, ...               # noqa: F401
from trading.eod_plan import eod_plan                                          # noqa: F401
from trading.order_state import _handle_order_update                           # noqa: F401
from trading.phases.pre_open import pre_open                                   # noqa: F401
from trading.phases.stop_loss import stop_loss_monitor                         # noqa: F401
from trading.phases.post_close import post_close                               # noqa: F401
from trading.phases.exit import place_take_profit                              # noqa: F401
```
消费方（broadcast/trading_service/orchestrate/tests）的 `from trading.engine import pre_open` 继续工作。

### 5.2 状态机语义不变形（[#5](../../architecture/05-state-machines.md) 红线）
订单/计划/持仓状态迁移逻辑**纯移动不改**：`_handle_order_update` 三分支语义、fill 表 `UNIQUE(order_id, traded_time)` 幂等、`_advance_order_state_from_status` 状态推进 —— 移到 order_state.py 时逐行原样，状态机权威归 #5。

### 5.3 数据路径不断（[#3](../../architecture/03-data-flow.md) 红线）
state_store 读写口、account_daily 熔断基线（W4 断链根治后唯一读口 `get_start_equity`）、trade_event/fill/position 落账点 —— 拆分仅改文件归属，不改读写口。

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| **循环 import**：phases/ import `TradingEngine` 类 ↔ engine import phases | EnginePorts 窄接口使 phases **不 import engine 类**（只 import `EnginePorts` + state_store 等模块级单例）→ 无循环；engine 单向 import phases |
| **APScheduler 早期绑定**：job 入口原是模块级函数 | 入口仍为 engine 的 `_pre_open` wrapper（深剖 §3.2 已是 wrapper 形态），wrapper 内 `await phases.pre_open(self._ports, date)`，APScheduler 绑定 wrapper 不变 |
| **`_ACTIVE_ENGINE` 消除遗漏**：5 处使用点漏改 | Step 1 专项处理 + 全量回归门兜底；消除后 `_ACTIVE_ENGINE` 与 `_dynamic_whitelist` 实例属性的桥接语义全收进 Ports |
| **回归覆盖不足** | 60+ tests/trading 单测 + tests/e2e_long_cycle 长周期基线，覆盖 10 集群；Step 10 终验双跑对比 |
| **H 并入 F 的备选** | 本 spec 选独立 exit.py（§2 裁定）；若 review 认为 F/H 共享内聚更强可改并入，影响面仅 Step 9 |

## 7. 范围边界（明确不做什么）

- **不做 T2 适配层**：不引入可插拔策略/资产/账户/经纪商抽象（T2 的事）；EnginePorts 是窄依赖接口不是适配层。
- **不改 state_store SSoT**：state_store 保持模块级单例访问形态，SSoT 重设计归 [T6](../../../plans/wayfinder/T6.md)。
- **不改策略算法**：颈线法信号/止损价计算逻辑不动（缺口在 neckline-algorithm-gaps memory，非架构债）。
- **不改 broker/qmt.py**：T2 裁定连接层不重构，本工单不动 broker。
- **不改数据流**：lake/state_store 读写口不变（§5.3）。

## 8. 验收标准（T1 完成）

- [ ] engine.py ≤ ~900 行（仅集群 J + C，深剖估 ~800）
- [ ] 8 个外迁文件就位，职责与 §2 表一致
- [ ] `_ACTIVE_ENGINE` 单例桥完全消除（grep 无残留）
- [ ] 公共 API 兼容：外部消费方 `from trading.engine import *` 零改动
- [ ] tests/trading 60+ 单测全绿
- [ ] tests/e2e_long_cycle 长周期回归双跑行为等价
- [ ] [#2](../../architecture/02-module-dependencies.md) 模块依赖图 + [#6](../../architecture/06-tech-debt.md) 技术债热力图同步更新（engine god module 项移除/降级）

## 9. 相关

- [T0.1 深剖](../../architecture/deep-dives/engine-current-state.md) —— engine 内部结构唯一权威（集群/缝合点/调用图）
- [T1](../../../plans/wayfinder/T1.md) —— 本工单
- [T2](../../../plans/wayfinder/T2.md) —— 解封目标（broker 适配层）
- [T6](../../../plans/wayfinder/T6.md) —— state_store SSoT 协同
- [#2 模块依赖](../../architecture/02-module-dependencies.md) / [#5 状态机](../../architecture/05-state-machines.md) / [#6 技术债](../../architecture/06-tech-debt.md) —— 上层视图
```
