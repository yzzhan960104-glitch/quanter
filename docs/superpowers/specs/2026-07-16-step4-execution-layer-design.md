# Step 4 · 执行编排层设计（trading/ 扩容 + caisen/infra 迁出 + 双源真理消除）

- **日期**：2026-07-16
- **分支**：`refactor/step4-execution-layer`（从 master `2ec9c89` 切出，承接已合并的 Step1/2/3 后端分层重构）
- **推进策略**：渐进 strangler（复用 Step3 的 sys.modules 别名垫片 + 预加载行模式，每子阶段 pytest 绿可中断）
- **状态**：设计中

---

## 1. 背景与动机

Step 1/2/3 已完成后端分层（config 拆包 + core 解散 + caisen 立 facade + engines/optimize/infra/advisor 四子包），但 **`caisen/infra/` 是过渡子包**——design §3.1 明示「Step4 移出 caisen 包」。Step4 处理被推迟的执行编排层，并修正 Step3 暴露/遗留的若干债务。

**用户决策**：当前处于参数调优阶段、实盘逻辑后续还要大改，**先把架构理顺**——Step4 的实盘风险点因"实盘逻辑待重写"而稀释，是结构先行的窗口期。

---

## 2. 现状诊断（证据）

### 2.1 反向依赖（模型层 → 服务层/执行层，分层违规）

```
caisen/infra/execution.py::ExecutionEngine(__init__:185)
  ├→ self.trading = trading_service        # server/services/trading_service（模型层→服务层!）
  │    └→ trading/{execution_gateway, risk_shield, qmt_market_data}
  ├→ storage.load_plans/update_plan        # caisen/infra/storage（计划状态机联动）
  └→ check_exit(pos, bar, bars_held, cfg)   # 本模块纯函数

caisen/infra/replay_worker.py:33
  └→ from server.services.caisen_service import _load_price_data, _merge_cfg  # 模型层→服务层!
      （靠 caisen_service 薄壳的 2 个兼容转发维持，Step2.2 过渡债）
```

ExecutionEngine 是「盘中执行编排」（ARMED→FILLED→CLOSED 状态机），物理位于 caisen/infra（模型层），却持有 server 层 trading_service——**最大的分层违规**。

### 2.2 双源真理（重要架构债，Step4 必须修正）

`check_exit` 离场纯函数有两份独立实现，靠注释手动"对齐"：

| 位置 | 用途 | 实现 |
|---|---|---|
| `caisen/infra/execution.py:92` `check_exit()` | 实盘 ExecutionEngine | 纯函数 `(pos, bar, bars_held, cfg) -> ExitDecision` |
| `caisen/infra/backtest_replay.py:318-348` | 训练态回放 | **独立逻辑**（止损/止盈_2x/时间止损优先级），注释「与实盘 check_exit(execution.py:142-148) 完全对齐」 |

**风险**：backtest_replay（训练调优依据）与 ExecutionEngine（实盘离场）是两份代码，任何一边改了另一边可能漏改 → **回测一套离场规则、实盘另一套**。这正是 design §1 要消除的"双源真理隐患"。

### 2.3 双 risk（已核对，互补两层非重复）

| 层 | 模块 | 职责 | 调用点 |
|---|---|---|---|
| 事前 | `caisen/engines/risk.py::RiskManager` | 头寸定权（Risk Parity）/ 流动性过滤 / 盈亏比 | screener/plan/facade/backtest_replay（计划生成时） |
| 事中 | `trading/risk_shield.py::check_order` | 废单 / 超限 / 熔断 / 涨跌停封板 | trading_service.submit_order（下单时） |

风控链：`计划生成(RiskManager 算头寸) → 审核 → ARMED → 下单(risk_shield 拦废单) → FILLED → 离场(check_exit)`。

### 2.4 三种执行态（已具雏形）

- **训练态**：`caisen/infra/backtest_replay.py`（纯函数撮合，无 gateway，无账本）
- **模拟态**：`trading/execution_gateway.py::MockExecutionGateway`
- **真实态**：`trading/emt_gateway.py::EmtExecutionGateway` / `trading/qmt_gateway.py::QmtExecutionGateway`
- **实盘执行核心**：`server/services/trading_service.py::submit_order`（get_gateway 按 env 路由 + check_order）

---

## 3. 目标架构

```
┌─ 执行编排层 execution/（新建顶层包）─────────────────────────────────┐
│  ExecutionEngine              ← caisen/infra/execution.py 迁入         │
│    (ARMED→FILLED→CLOSED 状态机, tick_pullback/tick_exit 驱动)         │
│  order_state                  ← trading/order_state.py（或留 trading） │
│  三执行态（撮合/账本真实性隔离）                                        │
│    ├ BaseExecutionGateway(ABC) ← trading/execution_gateway.py         │
│    ├ MockExecutionGateway      ← trading/execution_gateway.py         │
│    ├ EmtExecutionGateway       ← trading/emt_gateway.py              │
│    ├ QmtExecutionGateway       ← trading/qmt_gateway.py              │
│    └ backtest_replay           ← caisen/infra/backtest_replay.py(训练)│
│  异步回测                                                             │
│    └ replay_{runs,scheduler,tasks_db,worker} ← caisen/infra 迁入      │
│  计划状态仓储                                                         │
│    └ storage                   ← caisen/infra/storage.py             │
│  事中风控                                                             │
│    └ risk_shield.check_order   ← trading/risk_shield.py              │
└───────────────────────────────────────────────────────────────────────┘
        ↑ execution/ 依赖 trading/ 执行原语（gateway 实现）
        │
┌─ 执行原语 trading/（保留，gateway 底层实现 + qmt_market_data）──────────┐
└───────────────────────────────────────────────────────────────────────┘

┌─ 模型层 caisen/（Step4 后收敛为纯模型，零反向依赖）────────────────────┐
│  facade.py + engines/(plan/risk/patterns/config + check_exit 纯函数)  │
│  + optimize/ + advisor/                                              │
└───────────────────────────────────────────────────────────────────────┘

┌─ 横切 ────────────────────────────────────────────────────────────────┐
│  viz/(viz_static + viz_interactive 从 caisen/infra 迁入合并)         │
│  infra/notifier（Step1 已迁）                                         │
└───────────────────────────────────────────────────────────────────────┘
```

**依赖铁律（Step4 后）**：
- `execution/` → `trading/`（编排调原语）+ `caisen/engines/`（check_exit 单源）+ `data/`（行情）
- `caisen/` 零反向依赖 `execution/`、`trading/`、`server/`
- 接口层 `server/` → `execution/`（执行编排）+ `caisen/facade`（模型门面）

---

## 4. 关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 执行编排层位置 | **新建 `execution/` 顶层包** | ExecutionEngine（编排）与 trading/（原语）层次不同；execution/ 调 trading/，职责清晰。design §3.1 一致 |
| trading/ 去向 | **保留为执行原语层**（gateway 实现 + qmt_market_data） | gateway 是底层执行原语，与编排分开；execution/ 调用 |
| check_exit 归属 | **抽到 `caisen/engines/`（纯逻辑）**，backtest_replay + ExecutionEngine 共用 | 消除双源真理；离场是策略逻辑（纯函数），归 engines；执行层 import 它 |
| storage 归属 | **`execution/`** | 与 ExecutionEngine 状态机强耦合（ARMED/FILLED/active.json 联动） |
| 依赖反转 | ExecutionEngine 不再持有 `server/trading_service`；execution/ 内部装配 submit_order（gateway + risk_shield），或注入执行接口 | 消除模型层→服务层反向；execution/ 自含执行核心 |
| replay_worker 反向债 | `_load_price_data`/`_merge_cfg` 抽到共享位置（`data/price_loader.py` 或 facade 模块级），replay_worker 直接 import，删 caisen_service 兼容转发 | 消除模型层→服务层；保留测试 monkeypatch 语义（模块级名字） |
| 双 risk | **不物理合并**，明确"风控链"分层契约（事前 RiskManager 留 engines + 事中 risk_shield 留 execution） | 它们职责正交（算头寸 vs 拦废单），互补两层；合并反而破坏清晰度 |
| backtest_replay 抽象 | 纳入"执行态"语义（训练态），但**不强行塞进 BaseExecutionGateway** | 它是纯函数撮合无 gateway 语义；通过 check_exit 单源 + 行情统一实现"执行态统一"，非接口强行统一 |

---

## 5. 工作块

### A. 建执行编排层骨架（4a）
新建 `execution/` 包，`__init__.py` 从 caisen/infra + trading re-export（文件暂不动，新旧路径并存）。复用 Step3 的 sys.modules 别名垫片模式。

### B. check_exit 单源化（4b，先做·消除双源真理）
1. `check_exit` 从 `caisen/infra/execution.py:92` 抽到 `caisen/engines/exit_logic.py`（或 plan.py）。
2. `backtest_replay.py:318-348` 的独立离场逻辑**删除**，改调 `check_exit`（消除双源）。
3. ExecutionEngine + backtest_replay + tests 都从 engines import check_exit。
4. **风控拷问**：backtest_replay 现有离场（行 318-348）与 check_exit 必须行为等价（B-3 注释已对齐口径），迁移后跑 test_backtest_replay + test_execution 全绿确认零行为差异。

### C. ExecutionEngine + replay_* + storage 物理迁入 execution/（4c）
`git mv`：execution.py→execution/engine.py、replay_*.py→execution/、storage.py→execution/、backtest_replay.py→execution/。sys.modules 别名垫片 + caisen/__init__ 预加载兜底旧路径。

### D. 依赖反转（4d）
1. execution/ 内部装配 submit_order（从 trading_service 抽 gateway+risk_shield 装配逻辑），或定义执行接口让 server 注入。
2. ExecutionEngine 不再持有 server/trading_service。
3. server/services/trading_service 变薄（HTTP 适配 + 调 execution/）。

### E. 反向依赖/穿透收口（4e）
1. replay_worker：`_load_price_data`/`_merge_cfg` 抽共享位置（`data/price_loader.py`），删 caisen_service 兼容转发块。
2. server/api + celery_app 的 `from caisen.config/execution` 类型穿透 → facade re-export 或最终路径。
3. 清理 Step3 遗留的 10 垫片 + 预加载行（消费者切新路径后删，`test_shim_identity_tripwire` 绊线兜底）。

### F. viz 迁横切 + caisen 收敛终检（4f）
1. viz_static/viz_interactive → `viz/`（与顶层 viz/ 合并）。
2. caisen/ 收敛为 facade + engines + optimize + advisor（infra 整体迁出）。
3. 终检：caisen 零反向依赖 execution/trading/server（grep 验证）。

---

## 6. 子阶段（各自 pytest 绿可中断）

| 子阶段 | 内容 | 风险 |
|---|---|---|
| 4a | execution/ 骨架 + re-export | 低（纯骨架） |
| 4b | check_exit 单源化（消除双源真理） | 中（行为等价须验证） |
| 4c | ExecutionEngine + replay + storage 物理迁 execution/ | 中（import 链，复用 Step3 模式） |
| 4d | 依赖反转（ExecutionEngine 去 trading_service） | 高（实盘执行路径） |
| 4e | 反向债/穿透收口 + 垫片清理 | 中 |
| 4f | viz 迁横切 + caisen 收敛终检 | 低 |

---

## 7. 范围边界（明确不做）

- ❌ 不改策略算法/参数/风控阈值（含 JQDATA 配额、RiskManager 头寸系数、risk_shield 拦截阈值）。
- ❌ 不重写实盘 gateway（EMT/QMT）——只迁移位置 + 依赖反转。
- ❌ 不改 data_lake 存储 / parquet schema。
- ❌ 不改前端（web/）——仅后端结构。
- ❌ check_exit 单源化只做"行为等价合并"，不改离场规则本身。

---

## 8. 风控红线（实盘相关，最敏感）

1. **check_exit 行为等价**：backtest_replay 与 ExecutionEngine 合并用同一 check_exit 后，必须 test_backtest_replay + test_execution 全绿（零行为差异）。这是消除双源真理的核心验证。
2. **风控链不断**：事前 RiskManager + 事中 risk_shield 任一环缺失 = 实盘风险。4d 依赖反转后必跑 emt_smoke/qmt_smoke + test_risk_shield。
3. **三态隔离防串台**：训练态绝不调真实 gateway；get_gateway env 路由严格。
4. **emergency_halt 幂等**：一键熔断不能因 trading_service 改造破坏。
5. **T+1 底仓冻结感知**：A 股变相 T+0 合规底线不能丢。

---

## 9. 验证策略

| 子阶段 | 验证 |
|---|---|
| 4a | pytest 全绿 + execution/ 可 import |
| 4b | pytest + **test_backtest_replay + test_execution 专项全绿（行为等价）** |
| 4c | pytest + python -m caisen 冒烟 + 垫片同源绊线 |
| 4d | pytest + **emt_smoke + qmt_smoke（实盘风控链）** + test_risk_shield |
| 4e | pytest + caisen_service 零兼容转发 + server 零穿透 |
| 4f | pytest + **caisen 零反向依赖 grep 终检** + git diff master 形态（strangler） |

---

## 10. 开放决策记录

| 决策点 | 选择 | 理由 |
|---|---|---|
| check_exit 抽到 engines/ 而非 execution/ | 离场是策略纯逻辑（无 IO），归模型层；执行层调它 | 单源真理 + 引擎可独立测试 |
| execution/ 独立 vs trading/ 扩容 | 独立 execution/ | 编排与原语分层；execution/ 调 trading/ |
| 双 risk 不合并 | 职责正交（事前算头寸 vs 事中拦废单） | 合并破坏清晰度；明确风控链契约即可 |
| backtest_replay 不塞进 BaseExecutionGateway | 纯函数撮合无 gateway 语义 | 接口强行统一会扭曲；靠 check_exit 单源 + 行情统一实现"执行态统一" |
| storage 归 execution/ 而非数据层 | 与 ExecutionEngine 状态机强耦合 | active.json 是执行器高频读路径 |
