# Step 4 · 执行编排层实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。Steps use checkbox (`- [ ]`).

**Goal:** 新建 `execution/` 执行编排层，把 `caisen/infra/` 整体迁出 caisen 包；消除 check_exit 双源真理；反转 ExecutionEngine 对 server/trading_service 的反向依赖；caisen 收敛为纯模型层（零反向依赖 execution/trading/server）。

**Architecture:** 渐进 strangler（复用 Step3 的 sys.modules 别名垫片 + 预加载行模式）。execution/ 调 trading/（原语）+ caisen/engines/（check_exit 单源）；caisen/ 零反向依赖。详见 `docs/superpowers/specs/2026-07-16-step4-execution-layer-design.md`。

**Tech Stack:** Python 3.10、FastAPI、pytest（基线 827）、sys.modules 别名垫片。

---

## Global Constraints

- **语言**：全中文注释/commit message。
- **strangler 红线**：迁移实体文件零逻辑改动（仅位置 + import 路径）；每子阶段 pytest 绿 + commit + 可中断。
- **风控红线（实盘）**：check_exit 单源化必须行为等价（test_backtest_replay + test_execution 全绿）；风控链（事前 RiskManager + 事中 risk_shield）不断；emt_smoke/qmt_smoke 在 4d 后必跑。
- **不动**：策略算法/参数/风控阈值、JQDATA 配额、RiskManager 头寸系数、risk_shield 拦截阈值、data_lake schema、前端 web/。
- **复用 Step3 模式**：sys.modules 别名垫片（非 import\*）+ caisen/__init__ 预加载行；`test_shim_identity_tripwire` 绊线兜底。

---

## 关键事实基线（design 探索结论）

1. **check_exit vs backtest_replay 离场**：核心 4 优先级（止损/止盈_2x/止盈/时间止损）+ profit 口径 **完全等价**；**唯一差异**：check_exit 有移动止盈（`cfg.trailing_to_breakeven`，execution.py:126-133），backtest_replay 无。单源化后行为取决于回测 cfg 的 trailing 字段——**必须 test_backtest_replay 验证零差异**。
2. **ExecutionEngine**（caisen/infra/execution.py:170）持有 `trading_service`（server 层）+ 调 storage + check_exit。
3. **replay_worker**（caisen/infra/replay_worker.py:33）反向 `from server.services.caisen_service import _load_price_data, _merge_cfg`。
4. **双 risk 互补两层**：事前 `caisen/engines/risk.py::RiskManager` + 事中 `trading/risk_shield.py::check_order`，不合并。
5. **基线**：827 passed / 6 既有 warnings。

---

## Task 0：锁基线 + execution compat 测试骨架

**Files:** Create `tests/test_execution_layer_compat.py`

- [ ] **Step 1:** 跑 `pytest -q` 锁基线（827 passed）。
- [ ] **Step 2:** 建 `tests/test_execution_layer_compat.py` 骨架（贯穿 Step4 的执行层契约：旧路径可用 + 后续追加新路径同源 + caisen 零反向依赖绊线）。
- [ ] **Step 3:** commit `test(exec-layer): Task0 建 execution_layer_compat 骨架 + 锁基线827`。

---

## Phase 4a · execution/ 骨架 + re-export（文件暂不动）

**Files:** Create `execution/__init__.py`

- [ ] 建 `execution/__init__.py`：从 caisen/infra + trading re-export（execution/replay/storage/backtest_replay/gateway/risk_shield），新旧路径并存。复用 Task3.1 模式。
- [ ] 追加 compat 测试：`from execution import ExecutionEngine/check_order/BaseExecutionGateway` 可用 + 与旧路径同源。
- [ ] 全量 pytest ≥827 + commit `refactor(exec): Step4a 建 execution/ 骨架+re-export(新旧并存)`。

---

## Phase 4b · check_exit 单源化（消除双源真理·最高风险）

**Files:**
- Create `caisen/engines/exit_logic.py`（check_exit + ExitDecision/ExitAction/ExitReason 迁入）
- Modify `caisen/infra/execution.py`（check_exit 改 re-export exit_logic）
- Modify `caisen/infra/backtest_replay.py:318-353`（删独立离场逻辑，改调 check_exit）

**关键（行为等价验证）：**
- [ ] **Step 1:** 确认回测 cfg 的 `trailing_to_breakeven` 字段值（Read backtest_replay 调用链 + StrategyConfig 默认）。若回测 cfg trailing=False → 单源化后零行为差异；若 True → 需决策（回测是否引入移动止盈）。
- [ ] **Step 2:** `check_exit` + ExitDecision/Action/Reason 从 `caisen/infra/execution.py:92` 抽到 `caisen/engines/exit_logic.py`（纯逻辑归 engines）。execution.py 改 `from caisen.engines.exit_logic import check_exit, ExitDecision`（或 sys.modules 别名垫片保旧路径）。
- [ ] **Step 3:** `backtest_replay.py:318-353` 的独立离场逻辑**删除**，循环内改调 `check_exit(pos_dict, bar_dict, bars_held, cfg)`。字段名适配（p.stop_loss→pos["stop"] 等）。
- [ ] **Step 4:** **专项验证**：`pytest tests/caisen/test_backtest_replay.py tests/caisen/test_execution.py -v` 全绿（行为等价）。若红，定位是 trailing 差异 → 决策（保持回测 cfg trailing 设置使行为不变）。
- [ ] **Step 5:** 全量 pytest ≥827 + 追加 compat 断言（check_exit 单源：execution/backtest_replay/tests 同一函数对象）+ commit `refactor(engines): Step4b check_exit 单源化(消除backtest_replay双源真理,行为等价验证)`。

> **风控拷问**：回测是调优依据，行为等价是红线。Step 4 若发现 trailing 差异导致回测结果变，**必须**让用户决策（保持回测现状禁用 trailing，还是回测对齐实盘引入 trailing）——这是影响调优数据的决策点，不可擅自定。

---

## Phase 4c · ExecutionEngine + replay + storage 物理迁入 execution/

**Files:** `git mv` caisen/infra/{execution,backtest_replay,replay_runs,replay_scheduler,replay_tasks_db,replay_worker,storage}.py → execution/

- [ ] 分批 git mv（execution.py→execution/engine.py、backtest_replay/replay_*/storage→execution/），每批 sys.modules 别名垫片 + caisen/__init__ 预加载兜底。
- [ ] caisen/infra 顶层垫片（保旧路径 `from caisen.execution import` 可用，复用 Task3.2/3.3/3.4 模式）。
- [ ] 每批 pytest ≥827；**facade 仍绿**（facade import storage/backtest_replay/replay_runs/replay_tasks_db 经兜底）。
- [ ] commit `refactor(exec): Step4c 物理迁移 ExecutionEngine+replay+storage 进 execution/(sys.modules别名垫片)`。

---

## Phase 4d · 依赖反转（ExecutionEngine 去 server/trading_service）

**Files:** Modify `execution/engine.py`（ExecutionEngine）、`server/services/trading_service.py`、可能新建 `execution/executor.py`

**目标**：ExecutionEngine 不再持有 server/trading_service；execution/ 内部装配 submit_order（gateway + risk_shield）或定义执行接口。

- [ ] **Step 1:** 分析 ExecutionEngine 对 trading_service 的调用面（get_status/submit_order 等），定义执行接口（Protocol/ABC）或抽 submit_order 到 execution/。
- [ ] **Step 2:** ExecutionEngine 改依赖 execution/ 内部的执行接口（或 trading/ gateway 直接），删除 `self.trading = trading_service`（server 层注入）。
- [ ] **Step 3:** server/services/trading_service.py 变薄（HTTP 适配 + 调 execution/），保留 emergency_halt 幂等 + get_gateway env 路由。
- [ ] **Step 4:** 更新 scripts/smoke_caisen.py + celery_app beat（ExecutionEngine 装配方式变更）。
- [ ] **Step 5:** **实盘风控链验证**：`pytest tests/test_risk_shield.py tests/test_trading_service*.py -v` + 若环境允许 `python scripts/emt_smoke.py`/`qmt_smoke.py` 冒烟。
- [ ] **Step 6:** 全量 pytest ≥827 + commit `refactor(exec): Step4d 依赖反转(ExecutionEngine去trading_service,execution自含执行核心)`。

> **风控拷问**：4d 动实盘执行路径。emergency_halt 幂等、T+1 底仓冻结、风控链（RiskManager→risk_shield）任一破坏 = 实盘事故。Step 5 必须全绿 + 冒烟。若 emt_smoke/qmt_smoke 环境不具备（无实盘凭证），至少 test_risk_shield + test_trading_service 全绿 + 人工 review 执行链。

---

## Phase 4e · 反向债/穿透收口 + 垫片清理

**Files:** Modify `caisen/infra/replay_worker.py`（迁后在 execution/replay_worker.py）、`server/api/v1/caisen.py:441`、`server/celery_app.py:44`、`server/services/caisen_service.py`、删 caisen 顶层垫片

- [ ] `_load_price_data`/`_merge_cfg` 抽到 `data/price_loader.py`（或 facade 模块级），execution/replay_worker 直接 import；删 caisen_service 的 2 兼容转发块。
- [ ] server/api + celery_app 的 `from caisen.config/execution` → facade re-export 或最终路径。
- [ ] 清理 Step3 遗留垫片：消费者切 `caisen.{engines,optimize}.X` 新路径后，删 caisen 顶层垫片 + caisen/__init__ 预加载行（`test_shim_identity_tripwire` 绊线兜底，注意 infra 垫片 4c 后保留至 4f）。
- [ ] 全量 pytest ≥827 + commit `refactor(exec): Step4e 反向债收口(replay_worker去caisen_service)+穿透清理`。

---

## Phase 4f · viz 迁横切 + caisen 收敛终检

**Files:** `git mv` caisen/infra/viz_{static,interactive}.py → viz/；删 caisen/infra（空）

- [ ] viz_static/viz_interactive → `viz/`（与顶层 viz/ 合并）。
- [ ] caisen/infra 清空（infra/__init__.py 标注已迁出或删除）。
- [ ] **caisen 收敛终检**：`grep -rnE "from (execution|trading|server)" caisen/ --include="*.py"` 应零（caisen 零反向依赖）。
- [ ] 全量 pytest ≥827 + `python -m caisen --help` 冒烟 + git diff master 形态（strangler，无算法 diff）+ commit `refactor(exec): Step4f viz迁横切+caisen收敛终检(infra整体迁出)`。

---

## Self-Review

- [ ] Spec 覆盖：design §5 工作块 A-F 全对应（4a-4f）。
- [ ] 4b 行为等价：test_backtest_replay + test_execution 专项验证（check_exit 单源不引入回测行为变化）。
- [ ] 4d 实盘安全：风控链 + emergency_halt + T+1 不破坏。
- [ ] caisen 零反向依赖终检（4f）。

## 回退矩阵

每子阶段独立 commit，`git revert` 单步即可回退。4b/4d 风险最高，revert 后系统仍可用（strangler 铁律②）。
