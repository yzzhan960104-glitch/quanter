# Layer 2 五模块解耦设计 · 实验 / 外部券商 / 策略 / 交易 / 回测

> **状态：🟡 总纲草案【待用户复审】（2026-07-22 重写，覆盖原 layer3-trade-domain 误命名稿）**
> 本文件是跨会话交接总纲。机器人/新会话续聊时，从「§8 当前进度」读起即可。
> 相关记忆：`quanter-layer3-trade-domain`（待更名）、`quanter-backend-layering`、`quanter-detoify-6layers`
> 所有文件路径行号均经 codegraph + 四路勘察 agent 逐行核实（2026-07-22）。

---

## §0 分层总览

全仓按三层纵切：

| Layer | 模块 | 性质 |
|---|---|---|
| **Layer 1** | 数据 `data` | 基础设施：行情/基本面/Tushare 湖，纯底座 |
| **Layer 2** | **实验 / 外部券商 / 策略 / 交易 / 回测** | 业务核心层，**五个互相解耦的模块**（本设计対象） |
| **Layer 3** | 后台 `server` + 机器人 `bot` | 观测/运营层，须有防腐层隔离 Layer 2 |

**Layer 2 依赖拓扑（单向、无循环、边界清晰）：**

```
Layer 3:  后台(server) + 机器人(bot,含数据机器人:编排历史行情采集)
             │   └─(采集编排)─▶ broker ─取数─▶ data(落湖)   [要存的行情]
             ▼ (防腐层,防 server 焊死 trading)
Layer 2:  experiment(零依赖叶子) ◀──拉权重── trading
          strategies(信号) ──信号──▶ trading / backtest
          broker(券商) ◀──实时行情+下单── trading
          backtest(回测+参数训练) ──▶ trading.compute / strategies / data
          trading(编排+compute内核) ──▶ experiment / strategies / broker / data
             ▼
Layer 1:  data (被所有人依赖)
```

四条铁律：
1. **依赖单向**：`trading → experiment/strategies/broker/data`；`backtest → trading.compute/strategies/data`；`实验/券商/策略` 三角互不依赖，也不被回测依赖。
2. **experiment 是被拉的叶子**（`trading → experiment`），不是注入方——方向在勘察中订正，见 §2。
3. **模块间只走数据契约**（Signal / OrderRequest / PlannedOrder / TradePlan / ExitDecision），不 import 对方内部类。
4. `caisen` 上帝包被**拆解消失**——这是解耦的收尾标志，不是先验目标。

---

## §1 背景：为什么要解耦 Layer 2

四大致命问题（均已逐行验证）：

1. **上帝包 `caisen`（56 文件/5083 行）混 5 个关注点**：策略算法(patterns)、交易逻辑(engines)、参数训练(optimize)、回测编排(infra)、可视化(viz)。
2. **三条循环依赖**：`caisen↔execution`、`caisen↔strategies`、`caisen↔server`。其中 `caisen↔strategies` 坐实于 `caisen/facade.py:46` 模块级 import ↔ `strategies/caisen_pattern.py:20-25` 反向 import caisen 7 个符号。
3. **32 个 `sys.modules` 转发垫片**（strangler 中间债，非原估的 20；17 个直接服务交易域），真身已迁到 `execution/`、`caisen/engines/`、`viz/`、`caisen/optimize/`。
4. **回测寄生在执行层**：回测 driver（`execution/backtest_replay.py`）与盘中执行状态机（`execution/engine.py`）同住 `execution/` 包，违反"回测独立、稳定性隔离"。

**🟢 利好**：杀手不变量——`check_exit`（`caisen/engines/exit_logic.py:78`）已是纯函数，且回测/实盘已共用（Step4b 实现，`execution/engine.py:60-70` 声明）。双源真理在 caisen 形态出场链上**已解决**；compute 抽取的任务是"扩展"单源到颈线法出场 + 收口绕路脚本，不是"建立"。

---

## §2 关键决策（刻碑）

| # | 决策 | 真实理由 |
|---|---|---|
| D1 | **五模块按职责解耦**（非领域实体/非生命周期流水线） | 实验/券商/策略三角独立 + 交易/回测各自编排，边界最清晰 |
| D2 | **compute 方案 A**：纯决策函数留 `trading.compute` 子包，回测单向依赖它 | 回测/实盘共用同一份决策内核 = 物理性消灭双源真理；依赖方向 `回测→trading.compute` 满足"交易稳定不被回测变动污染"（回测折腾波及不到交易） |
| D3 | **行情按是否存储分流** | 实时行情（不存，止损/决策取现价）= 交易直调券商接口，不过 data；历史采集（落湖）= Layer3 数据机器人编排券商取数 → data 落湖。行情物理来自券商 API，故行情查询能力属 broker 模块 |
| D4 | **回测独立成模块**（与交易并行） | 稳定性隔离：交易链路求稳（实盘不能老动），回测求变（频繁增减脚本/调参），分开避免互相污染 |
| D5 | **experiment = 纯版本/权重配置中心**（用户 1A） | 零外部依赖已是 hard-won 资产（纯标准库叶子包），不破坏；参数训练归 backtest（它是回测的高级用法）；训练冠军人工 CLI 录入 experiment（保留人审环节） |
| D6 | **caisen 形态退役**（用户 2A） | 颈线法是唯一活跃策略，多形态（W底/头肩/三角形）早不用；退役即可走捷径断 `caisen↔strategies` 循环，caisen 瘦身一大块 |
| D7 | **依赖方向订正**：`trading → experiment`（拉），非 `experiment → trading`（推） | 勘察订正：`trading/engine.py:575` 主动调 `resolve_active()` 拉权重，experiment 永不在请求路径被反向调用 |

---

## §3 五模块边界与接口契约

### §3.1 数据 `data`（Layer 1，本次不动）

- **职责**：行情/基本面/Tushare 湖的存储与读取（`data/lake_fetcher.py`、`data/price_loader.py`、`data/tushare_sync.py` 等）。
- **行情存储归宿**：只存"要存的"（历史 bar）。实时行情不过本层。
- **被依赖**：所有 Layer 2 模块。

### §3.2 实验 `experiment`（守边界，基本不动）

- **职责**：实盘策略版本配置中心 + 权重管理（资金守恒 ≤1.0）。
- **现状（已就位）**：6 文件纯标准库叶子包，零外部依赖。`models.py`（状态机+`validate_weight_sum` 红线）/`store.py`（SQLite WAL+三道权重守恒闸）/`resolver.py`（scan 唯一入口，实时读不缓存）/`cli.py`。
- **对外接口**：`resolver.resolve_active() -> list[ActiveExperiment]`（唯一入口，被 `trading/engine.py:575` 单点局部 import）。
- **依赖**：仅标准库 + 自身。**禁止** import trading/strategies/caisen/execution/backtest/data。
- **守边界要点**：
  - 保持 `trading → experiment` 拉取方向，勿改成推送。
  - `signal_runner` 留 trading（依赖 `OrderRequest`，挪进 experiment 会引反向依赖）。
  - 资金守恒三道闸（promote/set_weight 排除自身/rollback 回 ACTIVE）逻辑不可只搬状态机不搬校验。

### §3.3 外部券商 `broker`（剥出立模块）

- **职责**：订单接口（下单/撤单/查持仓/查委托）+ **实时行情接口**（出接口前完成驼峰归一化+涨跌停注入清洗）+ 查资金。**不含**历史采集、交易编排。
- **现状**：gateway 全埋 `trading/`；`BaseExecutionGateway`（`trading/execution_gateway.py:138`）已抽象下单/撤单/查持仓，但**查资金/查行情未统一**（缺口）；券商代码本身是干净叶子（零反向依赖）。
- **迁移来源 → 目标**：
  - `trading/execution_gateway.py`（基类+契约+Mock异步）→ `broker/base.py` + `broker/mock.py`
  - `trading/qmt_gateway.py`（1097 行，唯一在用实盘）→ `broker/qmt.py`
  - `trading/qmt_market_data.py`（实时行情+清洗，已是出接口前清洗）→ `broker/qmt_quote.py`
  - `trading/mock_broker.py`（同步，回测撮合用）→ **留 backtest**，不进 broker
- **接口缺口（必补）**：`broker.base` 补 `async query_asset() -> Mapping` + `async get_quote(symbol)` 抽象方法（现 QMT 用 `query_asset`、EMT 用私有 `_fetch_asset`，行情是模块级自由函数，均未上提到基类）。
- **死代码（阶段0删）**：EMT 全套——`trading/emt_gateway.py`(652)、`emt_api_python/` SDK、`.env` 的 `EMT_*`、`server/services/trading_service.py:68-77` EMT 优先分支、相关 tests/scripts。
- **`reconcile()` 归属**：留 trading（风控对账语义，被 `reconcile_job`/server 依赖），**不进 broker**。
- **兼容**：`from trading.execution_gateway import` 全仓 20+ 处，剥出后必须在原路径留 re-export 垫片（strangler 铁律①）。

### §3.4 策略 `strategies`（断环 + 收口）

- **职责**：形态学/信号算法（颈线法唯一活跃）。对外只暴露**纯信号函数 + Signal 数据契约**。不知道交易/回测/broker 存在。
- **现状**：
  - 颈线法策略本体在 `scripts/neckline_method_v0.py`+`neckline_backtest.py`，`strategies/neckline_method.py` 用 `sys.path` hack 挂载——算法已解耦（零 caisen 依赖），只差归位。
  - `strategies/` 包已有实质代码：`base.py`(Strategy Protocol+TRADE_REQUIRED_KEYS)、`registry.py`(@register_strategy 装饰器)、`neckline_method.py`、`neckline_schema.py`。
  - `caisen/engines/patterns/`（W底/头肩/三角形/screener/registry/zigzag）+ `caisen/patterns/` 垫片包 → **D6 整体退役**。
- **迁移动作**：
  - 颈线法收口：`scripts/neckline_method_v0.py`+`neckline_backtest.py` → `strategies/neckline/`，删 `neckline_method.py:22-29` sys.path hack，改本包 import。
  - 建 `Signal` dataclass（`@dataclass(frozen=True)`）收敛现状两套 dict 字段（回测 `TRADE_REQUIRED_KEYS` vs 实盘 `scan_live` 字段），`scan_at`/`scan_live` 统一返回 `list[Signal]`，`signal_runner.build_orders_from_signals` 改读 dataclass 字段。
- **断环捷径（D6）**：删 `strategies/caisen_pattern.py`（自述阶段E删）+ `caisen/facade.py:46` → `caisen↔strategies` 循环立即消失。
- **依赖**：仅 pandas + 自身 +（颈线法）scripts 算法。**禁止** import trading/broker/execution/caisen。

### §3.5 交易 `trading`（编排 + compute 内核）

- **职责**：依赖 experiment(权重)+strategies(信号)+broker(行情/下单)+data，编排四触发点交易流程。内部含 `compute` 纯决策子包（被 backtest 单向依赖）。
- **内部结构（functional core / imperative shell，五层）**：
  ```
  trading/
  ├─ compute/   ② 纯决策函数（无 I/O、确定性）— 回测/实盘共用，杀手不变量
  ├─ state/     ③ reducer 式状态机（(state,event)→(state',commands)），只吃干净 event
  ├─ io/        ④ 副作用壳（下单/撤单/查持仓/查行情，只调 broker+data，只搬运不判定）
  ├─ orchestrate/ ⑤ 编排（eod_plan/pre_open/stop_loss/post_close + __main__）
  └─ types/     ① 纯数据契约（Order/Position/PlannedOrder/ExitDecision/OrderState）
  ```
- **杀手不变量**：`compute`（+`types`+`state`）对外部零依赖。回测 = 喂历史 bar/event 给 compute/state，**不经过 io/orchestrate**；实盘 = compute/state + 真实 io + orchestrate。→ 决策逻辑物理上只有一份。
- **当前最大债**：
  - `trading/engine.py:324-425` `stop_loss_monitor` 是唯一"判定+查价+查仓+下单"四缠热点（其余三触发点较干净）。
  - `post_close` 熔断**未连线**（`check_daily_loss_limit`+`cancel_all_open_orders`+`emergency_halt` 三步缺 equity 数据源未串，live 前必修）。
  - 状态机非 reducer 式：`execution/engine.py` 的 `tick_pullback`/`tick_exit` 把"读storage+查行情+判定+下单+写storage"混在一个 async 方法，判定纯但状态推进与 broker I/O 耦合。

### §3.6 回测 `backtest`（独立 + 收口双源）

- **职责**：历史回放验证 + 参数训练。变动频繁部分（driver/撮合/统计/参数搜索）集中于此，与交易稳定性隔离。
- **现状**：driver（`execution/backtest_replay.py`，干净、只依赖 `strategies.base.Strategy`）与盘中执行状态机同包；统计内联 driver；`param_iter.py`/`identify_param_scan.py` 直调 `neckline_backtest.scan_symbol`——**识别+模拟内核已同源**（Signal dataclass + `scan_symbol` 参数化，Task 1.6 收口，`test_scan_symbol_matches_strategy` 守护 `scan_symbol`≡`scan_at`）；统计层有意分轨（param_iter kelly 调参目标函数 vs replay CAGR 展示统计）—— 非债。全局 mutation 传参债已清（follow-up 2026-07-23 §3.2）。
- **迁移来源 → 目标**（拆 execution 包）：
  - `execution/backtest_replay.py`+`replay_worker.py`+`replay_scheduler.py`+`replay_tasks_db.py`+`replay_runs.py` → `backtest/`
  - `caisen/optimize/`（training_loop/training_analyzer/training_loops_db/training_dingtalk）→ `backtest/optimize/`（D5，参数训练归此）
  - `scripts/param_iter.py`+`identify_param_scan.py`+`calibrate_min_rr.py` → 收口走 driver，消灭双源路径
  - `trading/mock_broker.py`（同步撮合）→ `backtest/`（回测撮合用）
- **死包回收**：`backtest/` 现是 0 文件空壳（残留 `__pycache__`），名字直接复用，先 `git rm -r backtest/__pycache__`。
- **依赖**：`trading.compute`（单向）+ `strategies`（信号）+ `data`（历史 bar）。**禁止** import 整个 `trading/engine` 或 `execution`（盘中执行）。

---

## §4 trading.compute 纯函数归位表

13+ 候选纯函数（勘察核实），按目标归位。**保持 `check_exit` is 同源是红线。**

| 函数 | 现位置 | 纯度 | 目标 | 备注 |
|---|---|---|---|---|
| `check_exit` | `caisen/engines/exit_logic.py:78` | 100% | `compute/exit.py` | ★单源已就位，搬迁保持 is 同源 |
| `check_order` | `trading/risk_shield.py:41` | 100% | `compute/risk.py` | 事中风控 10 关 |
| `position_size` | `caisen/engines/risk.py:149` | 100% | `compute/position.py` | 颈线法 `signal_runner` 内联 qty 公式一并抽此 |
| `build_orders_from_signals` | `trading/signal_runner.py:36` | 100% | `compute/plan.py` | 颈线法计划生成 |
| `compute_stop_price` | `trading/stop_loss.py:15` | 100% | `compute/stop.py` | 海龟 trailing 离散化 |
| `check_stop_loss`/`check_take_profit`/`update_trailing_stop` | `trading/order_state.py:284-339` | 100% | `compute/` | 与 `check_exit` 命名统一（止损语义去重） |
| `reconcile` | `trading/execution_gateway.py:44` | 100% | `compute/reconcile.py` | 对账分类，留 trading |
| `check_daily_loss_limit` | `trading/circuit_breaker.py:46` | 纯判定 | `compute/breaker.py` | `cancel_all_open_orders` 进 io（副作用） |
| `check_pullback` | `execution/engine.py:119` | 纯 | `compute/` | ARMED→FILLED 触发判定 |
| `macro_position_coef` | `caisen/engines/risk.py:53` | 半纯 | 拆：`compute/regime_coef`纯 + io 取 regime 快照 | regime 读 macro 湖灰区，快照化 |
| `should_stop_loss` | 待抽（从 `stop_loss_monitor` 拆） | 将纯 | `compute/` | 拆四缠热点，或复用 `check_exit` |
| ~~`plan.generate`~~ | `caisen/engines/plan.py:115` | 半纯 | **随 caisen 形态退役删（D6）** | 只服务 caisen 形态 |
| ~~`micro_filter`/`liquidity_filter`~~ | `caisen/engines/risk.py:77/124` | 纯 | **待确认**：颈线法是否复用，否则随形态退役 | 见 §9 |

**命名冲突预警**：`check_exit` / `check_stop_loss` / `compute_stop_price` 三者皆"止损"语义但实现不同，归位时统一命名空间避免混淆。

---

## §5 execution 包拆分方案

`execution/` 当前混了回测 + 盘中执行，必须物理分离：

| 现文件 | 去向 | 备注 |
|---|---|---|
| `execution/backtest_replay.py` | `backtest/` | driver，策略中立 |
| `execution/replay_worker.py` | `backtest/` | |
| `execution/replay_scheduler.py` | `backtest/` | |
| `execution/replay_tasks_db.py` | `backtest/` | |
| `execution/replay_runs.py` | `backtest/` | |
| `execution/engine.py` | `trading/state/` | 盘中执行状态机，reducer 化 |
| `execution/storage.py` | `trading/state/` | active.json 持久化（与状态机同域） |
| `execution/interfaces.py` | `trading/`（依赖反转 Protocol） | `ExecutionExecutor` |
| `execution/__init__.py` | 拆解后**解散 `execution/` 包** | re-export 重整到各目标包 |

拆解后 `execution/` 作为独立包**消失**，其内容分流到 `backtest/` 和 `trading/state/`。原 `execution.*` 路径留垫片过渡。

---

## §6 strangler 迁移顺序（6 阶段，每阶段可独立合并）

> 原则：先低风险后高风险；先抽纯函数（回测立刻受益）后动 I/O；每阶段不破坏现状、可独立合并 + 验证。

**阶段 0 · 清死代码**（零风险）
- 删 EMT 全套（`emt_gateway.py` + `emt_api_python/` + `.env EMT_*` + `server trading_service.py:68-77` EMT 分支 + tests/scripts）
- `git rm -r backtest/__pycache__`
- 验证：测试绿、实盘仍走 QMT

**阶段 1 · 断环 + 策略收口（D6）**
- 删 `strategies/caisen_pattern.py` + `caisen/facade.py:46`
- 删 `caisen/engines/patterns/`（W底/头肩/三角形/screener/registry/zigzag）+ `caisen/patterns/` 垫片包
- 颈线法收口：`scripts/neckline_*.py` → `strategies/neckline/`，删 sys.path hack
- 建 `Signal` dataclass 收敛两套字段
- 验证：`caisen↔strategies` 循环消失（import-linter）、颈线法回测/实盘识别一致

**阶段 2 · 抽 trading.compute 子包（核心，回测受益）**
- 按 §4 归位 13+ 纯函数到 `trading/compute/`
- **保持 `check_exit` is 同源**（回测/实盘仍指向同一函数对象）
- 颈线法出场双源收口：抽公共离场纯函数，回测 `simulate_exit` 与实盘出场共用
- 验证：compute 子包零外部依赖、回测改依赖 `trading.compute`

**阶段 3 · 剥出 broker 模块**
- `execution_gateway.py`(基类+契约)+`qmt_gateway.py`+`qmt_market_data.py`+Mock异步 → `broker/`
- 补 `broker.base` 的 `query_asset`/`get_quote` 抽象
- 留 `trading.*` re-export 垫片（20+ 处消费）
- 验证：broker 零反向依赖、实盘 QMT 下单/行情不变

**阶段 4 · 拆 execution + 回测独立**
- 回测 5 文件 → `backtest/`；`caisen/optimize` → `backtest/optimize/`（D5）；`mock_broker.py` → `backtest/`
- `param_iter`/`identify_param_scan`/`calibrate_min_rr` 收口走 driver，消灭双源路径
- `execution/engine.py`+`storage.py` → `trading/state/`（reducer 化起步）
- 解散 `execution/` 包，留垫片
- 验证：回测独立成包、回测↔交易只通过 `trading.compute`

**阶段 5 · 交易内部定型（functional core 收口）**
- `trading/{types,compute,state,io,orchestrate}` 五层定型
- 拆 `stop_loss_monitor` 四缠热点（compute 判定 + io 查价查仓 + orchestrate 调度）
- `post_close` 熔断连线（补 equity 数据源，`check_daily_loss_limit`+`cancel_all_open_orders`+`emergency_halt` 串联）—— live 前必修
- 状态机 reducer 化（`result.state` 当 event 喂纯 reducer）
- 验证：io/orchestrate 内无业务判定（只错误处理分支）、状态机可纯单测

**阶段 6 · 收尾**
- 清剩余 sys.modules 垫片（逐个确认无消费后删）
- 立 `import-linter` 或 `tests/test_layer_contract.py` 守 §7 依赖铁律
- `caisen` 上帝包最终解散（config/plan/risk 剩余纯函数已迁 compute 后）
- 验证：全仓无循环依赖、caisen 包消失

---

## §7 依赖铁律（CI 必须守）

```
orchestrate ─► io ─► broker / data              （shell，可有副作用）
orchestrate ─► state ─► compute ─► types         （core，纯）
                 state ─► compute（迁移中可调决策）
backtest ─► trading.compute / strategies / data
trading ─► experiment / strategies / broker / data

禁止：compute/state import broker/data/io/orchestrate
禁止：io/orchestrate 内出现业务判定（只允许错误处理分支）
禁止：experiment import 任何 Layer 2 兄弟 + trading
禁止：strategies import trading/broker/execution/caisen
禁止：broker 反向 import trading 编排
禁止：backtest import 整个 trading/engine 或 execution（只许 trading.compute）
```

执行：`import-linter`（主）或 `tests/test_layer_contract.py`（扫 import 方向）。没有它，半年后又长出 god-module。

---

## §8 E2E 验证策略（🩸零回归红线的可执行兑现）

> pytest 全绿只证明「零件没坏」，不证明「整车还能开」。本重构是**纯结构迁移（零逻辑改动）**，
> 故 E2E 保证可锐化为一句：**迁移前后，黑盒输出逐数值 / 逐字节一致**。据此设四层验证，由廉到贵，
> 每阶段按「功能受影响面」选跑（plan 的「E2E 验证矩阵」节给出每阶段必跑项与验收标准）。

### §8.1 四层验证（T0–T3）

| 层 | 名称 | 抓什么 | 成本 | 现有设施 |
|---|---|---|---|---|
| **T0** | 单元/契约 | 零件级正确（函数/契约/同源） | 秒级 | 全量 pytest（基线 `918 passed / 3 failed`） |
| **T1** | **数值回归** | **决策内核级**：喂固定 `(bars, params)` → 固定 `(signals, trades, kelly 年化)` | 快版秒级 / 全版 8h | `param_iter.run_one`（快版·3 固定标的）+ `param_iter.py --time-budget 28800`（全版·8h 里程碑） |
| **T2** | 真实链路冒烟 | 链路级：编排/券商端到端连通 | 分钟级（部分需柜台/客户端） | `scripts/smoke_trading_engine.py`（交易编排影子 eod_plan 全链路）+ `scripts/qmt_live_smoke_headless.py`（券商 T1–T6 真实柜台 14 项） |
| **T3** | 浏览器 E2E | 表现层：前端不炸 | 分钟级 | `tests/e2e/lab_param_lab.py`（Playwright headless chromium） |

> **T1 是本重构最强的 E2E**：纯重构 = 决策逻辑物理不变，所以固定输入的输出**必须逐位一致**。
> 任何数值漂移 = 某处偷偷改了逻辑（违背 strangler「纯结构迁移」红线），立即报警。

### §8.2 功能 × 层 矩阵（所有当前已有功能）

| 功能 | 所属层 | 受影响阶段 | T0 | T1 数值 | T2 链路冒烟 | T3 浏览器 |
|---|---|---|---|---|---|---|
| 颈线法信号识别 `scan_at`/`scan_live` | 策略 | 1,2 | ✓ | ✓✓ **核心** | —（T1 已代） | — |
| 颈线法回测 driver + param_iter | 回测 | 2,4 | ✓ | ✓✓ **核心** | — | — |
| 实盘四触发点 eod/pre_open/stop_loss/post_close | 交易 | 2,3,5 | ✓ | — | ✓ `smoke_trading_engine` | — |
| QMT 下单/撤单/查持仓/查行情 | 券商 | 3 | ✓（mock） | — | ✓✓ `qmt_live_smoke_headless` | — |
| 实验系统 `resolve_active`（版本/权重） | 实验 | 否（守边界） | ✓ | — | 配置快照断言（入 pytest） | — |
| 后台 server API | Layer3 | 间接 | ✓ `tests/server` | — | ⚠ 缺独立 smoke（gap3） | — |
| Parameter Lab 前端 | Layer3 | 间接（signal 字段） | — | — | — | ✓ `lab_param_lab` |
| 钉钉播报 broadcast | Layer3 | 间接 | ✓ `tests/broadcast` | — | ⚠ 缺独立 smoke（gap3） | — |

### §8.3 诚实缺口（必须显式承认，不可假装全覆盖）

1. **颈线法无独立端到端冒烟**：现有 `smoke_caisen.py` 验的是 **caisen 形态**流水线（阶段 1 随 D6 退役删除），**不是颈线法**。颈线法 E2E 由 **T1 数值回归代**——`param_iter.run_one` 跑的就是颈线法 `scan_symbol` 全链路（识别→执行→凯利），数值一致即全链路未坏。**不新建颈线法 smoke**（YAGNI，T1 已覆盖）。
2. **`smoke_caisen.py` + `tests/e2e/caisen_replay_tab.py` + `tests/e2e/caisen_token_path.py` 随阶段 1 删除**：它们验的是即将退役的 caisen 形态 API，**不是稳定回归门**，是「随退役删」项，**不得计入"必须保持通过"的基线**。阶段 1 删除时一并清。
3. **broadcast / server 无独立 smoke**：属 Layer 3，本 Layer 2 plan 用 `tests/broadcast`（5 文件）+ `tests/server`（2 文件）pytest 兜底；正式 smoke 留 Layer 3 后台防腐层 plan（§10 遗留#5）。
4. **数值 golden 基线未捕获**：T1 快版需要「迁移前的 golden 数值」做对比锚。**阶段 1 Task 1.0 前置**：跑 T1 快版（3 固定标的 + DEFAULTS），记 golden kelly 年化，commit 进仓库（golden json 或 conftest 常量）。迁移后逐位对比。

### §8.4 T1 守护的三条关键不变量（数值一致 = 不变量未被破坏）

- **阶段 2 · `check_exit` is 同源**：回测 `simulate_exit` 与实盘出场共用同一函数对象 → T1 数值一致。破坏同源 = 数值漂移。
- **阶段 4 · 内核同源（统计层分轨是设计）**：识别+模拟内核同源由 `test_scan_symbol_matches_strategy` + `test_param_iter_kernel_same_source` 守护（Signal dataclass + `scan_symbol` 参数化，Task 1.6 收口）；统计层分轨是设计（param_iter kelly 调参 vs replay CAGR 展示）。T1 golden 守 param_iter 改传参后数值零漂移。
- **阶段 1 · Signal dataclass 收敛**：`scan_at`/`scan_live` 字段统一 → T1 数值一致 + T3 Lab 前端不炸。

---

## §9 当前进度（机器人续聊入口）

- [x] §0 分层总览（Layer 1/2/3）
- [x] §1 架构评审（caisen 上帝包 + 3 循环 + 32 垫片 + 回测寄生）
- [x] §2 七决策刻碑（D1-D7，含 compute 方案 A / experiment 1A / caisen 形态 2A）
- [x] §3 五模块边界与接口契约
- [x] §4 compute 纯函数归位表（13+）
- [x] §5 execution 包拆分方案
- [x] §6 strangler 6 阶段迁移顺序
- [x] §7 依赖铁律
- [x] §8 E2E 验证策略（T0–T3 + 功能矩阵 + 诚实缺口）← 2026-07-22 补
- [x] **业务总纲 → 用户复审通过**（2026-07-22「业务内容没问题了」，同步追加 E2E 要求）
- [ ] 转 writing-plans，从阶段 1（Task 1.0 起）执行
- [ ] §10 待裁决项逐个敲定（边迁边定）

**机器人续聊时**：本总纲（含 §8 E2E）已复审通过，按 plan `2026-07-22-layer2-decoupling-plan.md` 的「E2E 验证矩阵」+ 当前阶段 Task 执行，每步 Run+Expected 验证后勾 checkbox + commit。

---

## §10 待裁决 / 待确认遗留（边迁边定）

1. **颈线法是否复用 `caisen.engines.risk.RiskManager`**：`micro_filter`/`liquidity_filter`/`macro_position_coef` 是 caisen 形态筛选用。颈线法走 `build_orders_from_signals`，疑似不用。若不用 → 随 caisen 形态退役删；若用 → 抽进 compute。**阶段 1 起步时核实**。
2. **颈线法出场双源收口方式** · **🟢 已定案（2026-07-23）**：已由 Task 1.6 Signal dataclass + `scan_symbol` 参数化收口**内核**（识别+模拟同源，`test_scan_symbol_matches_strategy` 守护 `scan_symbol`≡`scan_at`）；统计层分轨定案（param_iter kelly 调参 vs replay CAGR 展示，有意分轨非债）。原「回测 `simulate_exit`（K 线 high/low 判定）与实盘 `pre_open+stop_loss_monitor` 语义不同」语境已被重新定义——内核同源即收口达成，统计层差异是业务诉求而非债。详见 follow-up 2026-07-23 §3。
3. **`post_close` 熔断数据源**：`check_daily_loss_limit` 需要 equity，但 broker 无 `get_equity` 接口。补在 broker.base 还是走 data 查账户快照。**阶段 3/5**。
4. **`viz/`（可视化）归属**：caisen 的 `viz_static`/`viz_interactive` 是横切可视化层，不在五模块内。倾向归 Layer 3（机器人/后台展示）或独立 viz 模块。**阶段 6 收尾定**。
5. **Layer 2/Layer 3 防腐层**：`server/` 直 import `trading.risk_shield`/`dynamic_whitelist`/`execution_gateway`/`caisen.facade` 是 `caisen↔server` 循环根因。属 Layer 3 后台防腐层债，**不在本 Layer 2 设计范围**，但阶段 6 须立防腐层收口。
