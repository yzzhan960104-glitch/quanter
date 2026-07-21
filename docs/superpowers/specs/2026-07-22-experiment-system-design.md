# 实验系统（Experiment System）· 设计文档

> **维护范围**：新增 `experiment/` 包 + `strategies/base.py` 协议扩展 + `execution/engine.py` 出场路由改造 + `execution/storage.py` plan 加归因字段 + 颈线法/cainen 实盘 scan 与出场适配
> **创建日期**：2026-07-22
> **状态**：设计待审 → 实现计划（writing-plans）

---

## 0. 一句话定位

**实验系统是实盘下单的「策略版本配置中心」**：管理「在线实验版本 + 资金权重」，scan 时由 resolver 给出当前线上/灰度的 `(strategy_name, params, weight)` 列表，策略模块据此解析算法执行。线上参数以不可变快照锁定（保障交易质量），灰度以资金权重分流，版本与 plan 归因全持久化。

---

## 1. 背景与动机

### 1.1 触发场景

即将开始 **miniQMT 虚拟盘** 真实下单（EMT 已废弃，见记忆订正）。实盘对「策略及参数稳定性」的要求远高于回测——回测里随手改 `EXEC_DEFAULTS` 重跑无代价，实盘改一个参数就可能导致全市场持仓逻辑剧变。

### 1.2 当前痛点（实盘链路盘点）

- **参数硬编码**：当前实盘 scan 走 `caisen_service`，`StrategyConfig` 硬编码在 `caisen/facade.py`，无配置中心，改参数=改代码=手滑风险。
- **`strategies/` 解耦红利未延伸到实盘**：2026-07-20 重构把策略与回测引擎解耦（`strategies/base.py` + `registry.py`），但**只被回测用**，实盘 scan 仍走 caisen 包老路；颈线法（唯一活跃策略）**还没进实盘 scan**。
- **无版本/灰度/回滚概念**：grep 全仓零 `experiment/gray/feature_flag/canary` 命中。参数一改就覆盖，无审计、无灰度、无逃生通道。
- **实盘出场引擎未解耦**：`execution/engine.py`（ExecutionEngine）的 `tick_exit` 硬调 caisen 的 `check_exit`，颈线法的分级止盈（tp1/tp2）+ trailing 移动止损无法在实盘表达。

### 1.3 实验系统的职责边界

| 做 | 不做 |
|---|---|
| 管理「在线实验版本 + 资金权重」（不可变快照 + 状态机 + 审计） | 不生成 plan、不下单（scan/execution 层的事） |
| `resolve_active()` 给出当前生效 `(name, params, weight)` | 不记账、不管持仓归因（券商合并账户的虚拟记账是 execution 层职责） |
| plan 落盘时携带 `experiment_id`+`weight` 归因字段 | 不实时算实验 PnL 指标（事后 `report` 命令离线扫 `plans/*.json` 聚合） |

---

## 2. 目标与非目标

### 2.1 MVP 目标

1. **配置中心**：`experiment/` 独立包，SQLite 持久化（版本 + 审计），CLI 管理（create/promote/set-weight/archive/rollback/list/report）。
2. **scan resolver 注入**：scan 启动调 `resolve_active()`，遍历在线实验 → `build_strategy` → 实盘 scan → 生成带 `experiment_id` 的 ARMED plan。
3. **颈线法完整接入实盘 scan**：`strategies/neckline_method` 实现 `scan_live` + `to_armed_plan` + `check_pullback` + `check_exit`（复用回测 `simulate_exit` 内核）。
4. **ExecutionEngine 出场路由解耦**：按 `plan.experiment_id` 路由到对应 Strategy 的 `check_exit`，caisen 零行为回归。
5. **双链路验收**：Mock + miniQMT 虚拟盘都能跑通端到端（create exp → scan → plan → tick → FILLED/CLOSED，归因不断链）。

### 2.2 非目标（MVP 外，follow-up）

- Parameter Lab 冠军参数「一键发布」UI（MVP 用 CLI，参数从 ParamLab 复制粘贴）
- 实时实验 PnL 看板 / 自动熔断 archive（MVP 仅 `report` 离线聚合 + 人工决策）
- `execution/storage.py` 整体迁 SQLite（MVP plan 仍 JSON，仅加归因字段；storage 全迁独立立项）
- caisen 阶段E 完整删除（颈线法已唯一活跃，caisen 代码保留作回归兜底）

---

## 3. 核心概念与数据模型

### 3.1 核心抽象：在线实验版本 + 资金权重

**不区分 prod/candidate 语义**——实验平台只懂「版本 + 权重」：

- 一个 **ExperimentVersion** = 策略名 + **参数快照（promote 后不可变）** + 资金权重 + 状态
- 多版本可同时 `ACTIVE`，**所有 ACTIVE 版本 weight 之和 ≤ 1.0**（资金守恒红线）
- 典型：纯线上 = 1 个 ACTIVE @100%；灰度 = 2 个 ACTIVE @80/20；扶正 = candidate → 100% + 旧 prod archive

### 3.2 ExperimentVersion 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `experiment_id` | TEXT PK | 唯一标识，如 `neckline_v6_20260722` |
| `strategy_name` | TEXT | `neckline` / `caisen`（`build_strategy` 的 name） |
| `params` | TEXT(JSON) | **不可变参数快照**（promote 后锁，防手滑） |
| `weight` | REAL | 资金占比 0.0~1.0 |
| `status` | TEXT | `DRAFT` / `ACTIVE` / `ARCHIVED` |
| `version` | INT | 同 `strategy_name` 下递增 |
| `source` | TEXT | `param_lab:run_xxx` / `manual` / `rollback` |
| `created_at` / `activated_at` / `archived_at` | TEXT(ISO) | 生命周期时间戳 |
| `note` | TEXT | 可选说明 |

### 3.3 状态机

```
DRAFT ──promote(weight)──→ ACTIVE ──archive──→ ARCHIVED
  │                           │                     │
  └──discard──→ (删除)    set-weight(不改status)    └──rollback──→ ACTIVE
```

**合法迁移**：DRAFT→ACTIVE(promote)、ACTIVE→ARCHIVED(archive)、ARCHIVED→ACTIVE(rollback)、DRAFT→删除(discard)、ACTIVE 内 set-weight（不改 status）。
**非法迁移一律拒绝**（如 ARCHIVED→DRAFT、已 ACTIVE 再 promote）。

### 3.4 AuditLog（append-only）

每次 create/promote/set-weight/archive/rollback/discard 写一条：`audit_id / timestamp / action / experiment_id / changed_fields(JSON，如 {"weight":[0.8,0.2]}) / operator / note`。

### 3.5 SQLite Schema（`experiment/experiments.db`，复用 `execution/replay_tasks_db` 范式）

```sql
CREATE TABLE experiment_version (
  experiment_id TEXT PRIMARY KEY, strategy_name TEXT NOT NULL,
  params TEXT NOT NULL, weight REAL NOT NULL, status TEXT NOT NULL,
  version INTEGER NOT NULL, source TEXT, note TEXT,
  created_at TEXT NOT NULL, activated_at TEXT, archived_at TEXT,
  UNIQUE(strategy_name, version));
CREATE INDEX idx_status ON experiment_version(status);

CREATE TABLE audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
  action TEXT NOT NULL, experiment_id TEXT NOT NULL,
  changed_fields TEXT, operator TEXT, note TEXT);
CREATE INDEX idx_audit_exp ON audit_log(experiment_id);
```

并发写用 SQLite 事务（WAL 模式），`store.py` 每次 promote/set-weight 在单事务内完成「更新版本表 + 写审计」，失败整体回滚。

### 3.6 plan 归因字段（`execution/storage.py` 加，底层 JSON 不动）

`_plan_to_dict` / `_restore_plan_dict` 增加：
- `experiment_id: str | None`（pre-experiment 老 plan 为 None，聚合时归「未归因」桶）
- `experiment_weight: float | None`（plan 落盘时冻结的权重，CLI 改权重不影响已落盘 plan）

归因聚合：`report` 命令扫 `plans/*.json` 按 `experiment_id` 分组算 PnL/胜率/回撤。

---

## 4. 架构与组件

### 4.1 新增 `experiment/` 包

```
experiment/
  __init__.py     # 导出 resolve_active / 公开 API
  models.py       # ExperimentVersion / AuditLog dataclass + ExperimentStatus 枚举 + 状态机校验
  store.py        # SQLite 持久化（experiments.db），WAL + 事务，复用 replay_tasks_db 范式
  resolver.py     # resolve_active() → [ActiveExperiment]；scan 唯一入口，实时读不缓存
  audit.py        # 变更审计写入 + 查询
  cli.py          # python -m experiment create|promote|set-weight|archive|rollback|list|report
```

### 4.2 依赖方向（零反向依赖红线）

```
strategies/ ←── build_strategy(name,params) ──← experiment/ (resolver, 只读 SQLite)
   ↑                                              ↑
   │ 实例化                                         │ 读
scan_service (caisen_service 改造) ──resolve_active()──┘
   │ 生成 ARMED plan（带 experiment_id + weight）
   ↓
execution/storage.py (plans/<date>.json)  ← plan 落盘（加归因字段）
   ↓
execution/engine.py (ExecutionEngine) ── 按 plan.experiment_id 路由 Strategy ──→ trading/ (Mock/QMT gateway)
```

**`experiment/` 零依赖** strategies/execution/trading/server —— 纯配置层，可独立单元测试。scan_service 依赖 experiment（resolve）+ strategies（build_strategy）。

### 4.3 注入点

`scan_service.run_scan(date)` 开头调 `resolver.resolve_active()`，结果为空则 fail-fast（无在线实验不下单）。

---

## 5. 数据流

### 5.1 实盘 scan（schtasks/CLI 每日触发）

```
scan_service.run_scan(date)
  ├─ 1. resolver.resolve_active()                      ← 实时读 SQLite，返 ACTIVE 且 weight>0
  │     → [ActiveExperiment(exp_id, strategy_name, params, weight), ...]
  ├─ 2. for each ActiveExperiment:
  │     strategy = build_strategy(exp.strategy_name, cfg_override=exp.params)
  │     for signal in strategy.scan_live(date):         ← 颈线法：聚集带突破+回踩挂单点
  │       plan = strategy.to_armed_plan(signal, weight=exp.weight, experiment_id=exp.exp_id)
  │       storage.save_plans(date, [plan])              ← 落 plans/<date>.json，带 exp_id+weight
  ├─ 3. [人工/自动审核] storage.update_plan(plan_id, status="ARMED")
  └─ 4. ExecutionEngine.tick_pullback/tick_exit → load_plans → 路由 Strategy 决策 → submit_order
```

### 5.2 CLI 操作流

```bash
python -m experiment create  --strategy neckline --params '{...}' --source "param_lab:run_xxx"
python -m experiment promote <exp_id> --weight 0.2          # DRAFT→ACTIVE，校验权重和≤1.0
python -m experiment set-weight <exp_id> --weight 0.5       # 灰度扩量（记审计）
python -m experiment archive <old_prod_id>                  # 下线
python -m experiment rollback <archived_id>                 # 回滚（ARCHIVED→ACTIVE）
python -m experiment list [--status active]
python -m experiment report --since 2026-07-01              # 归因聚合（扫 plans/*.json 按 exp_id）
```

### 5.3 权重热生效（零常驻进程）

`resolve_active()` 每次 scan **实时读 SQLite 不缓存**。CLI 改权重 → 写 SQLite+审计 → **下一次 scan 自动生效**，无需重启。scan 是 schtasks/CLI 触发的短任务，天然无缓存一致性问题。

### 5.4 一致性边界

- **权重变更 vs in-flight plan**：plan 落盘时 `experiment_weight` **冻结**。CLI 改权重只影响之后新 scan 的 plan；已 ARMED/FILLED 的 plan 按落盘时权重执行到底。
- **多实验同标的**：prod/candidate 都扫到标的 S → 各生成独立 plan（不同 exp_id）→ ExecutionEngine 分别消费。持仓合并是券商账户层的事，平台不管。
- **weight=0**：resolve 过滤 `weight>0`，权重调 0 = 软下线（不 archive，留审计）。

---

## 6. 颈线法实盘接入

### 6.1 架构决策：出场归策略侧（遵循 `strategies/base.py` 既有原则）

`base.py` 顶部钉死：「**出场逻辑归属：策略侧（核心架构决策）**……引擎不感知策略内部如何识别/进场/出场」。故出场适配**不靠 ExecutionEngine 学会颈线法**，而是**扩展 Strategy 协议**，颈线法/cainen 各自实现 tick 决策，ExecutionEngine 只调度。

### 6.2 Strategy 协议扩展（`strategies/base.py`，加 4 个实盘方法）

```python
class Strategy(Protocol):
    # —— 回测既有（不变）——
    def precompute(self, symbol, full_df) -> dict: ...
    def scan_at(self, symbol, df_T, T, strategy_state) -> list: ...
    @property
    def config_schema(self) -> type: ...

    # —— 实盘新增 ——
    def scan_live(self, date) -> list[Signal]: ...
        # 实盘识别：复用 scan_at 的识别内核（detect_neckline_method + search_neckline），
        # 只产出「聚集带突破 + 回踩挂单点」Signal，不模拟出场。

    def to_armed_plan(self, signal, *, weight, experiment_id) -> dict: ...
        # Signal → ARMED plan（挂单区间/shares/stop/tp1/tp2/trailing/max_wait，params 从 cfg 拿）

    def check_pullback(self, plan, quote, bars_armed) -> PullbackDecision: ...
        # ARMED 阶段：触及 [entry_lower,entry_upper]→FILLED；颈线法另含 max_wait 超时撤单

    def check_exit(self, plan, bar, bars_held) -> ExitDecision: ...
        # FILLED 阶段：颈线法 tp1部分+tp2全+trailing+超时；caisen 复用 exit_logic.check_exit
```

**Signal 字段**：`symbol / neckline_price / H(形态高度) / ATR / breakout_date / 形成日 T`（颈线法识别内核输出）。

### 6.3 决策对象扩展（支持分级平仓/撤单/trailing）

```python
class PullbackAction:   ARMED_FILL | CANCEL_TIMEOUT | HOLD
class ExitAction:       CLOSE_PORTION | CLOSE_ALL | UPDATE_STOP | HOLD
@dataclass
class ExitDecision:
    action: ExitAction
    portion: float = 1.0          # CLOSE_PORTION 时 = tp1_portion（如 0.5）
    new_stop: float | None = None # trailing 收紧后的新止损
    reason: str = ""              # stop_loss/take_profit_1/take_profit_2/trailing/timeout
```

### 6.4 颈线法 `to_armed_plan` 字段映射

| ARMED plan 字段 | 来源（颈线法 EXEC_DEFAULTS） |
|---|---|
| `entry_upper`/`entry_lower` | 颈线位回踩挂单区间（`buy_limit_atr_mult` × ATR） |
| `shares` | `weight × 总资金 × pos_cap / entry_upper`（**资金权重在此落地**） |
| `stop` | 颈线 − `stop_atr_mult` × ATR |
| `take_profit`（tp2 全止盈） | 颈线 + `tp_h_mult` × H |
| `take_profit_1`（tp1 部分） | 颈线 + `tp1_h_mult` × H |
| `tp1_portion` | `tp1_portion`（如 0.5） |
| `max_wait_bars` | `max_wait`（回踩挂单有效期，超时撤单） |
| `trailing_grace/step/floor` | trailing 移动止损 3 维 |
| `experiment_id`/`experiment_weight` | 归因 + 冻结权重 |

**shares 计算的「总资金」来源**：scan_service 在调 `to_armed_plan` 前，从 `trading_service`/gateway 查询账户可用资金（miniQMT `queryAsset` / Mock `initial_cash`），作为 `total_capital` 注入；`shares = weight × total_capital × pos_cap / entry_upper`，向下取整到 100 股（A 股最小交易单位）。weight 在此落地为实际股数。

### 6.5 颈线法 `check_exit`（= 回测 `simulate_exit` 实盘版）

```
优先级：stop_loss > tp1(部分,首次) > tp2(全平) > trailing 收紧 > max_holding 超时
trailing：bars_held > grace → eff_mult = max(stop_mult − (bars_held−grace)×step, floor) → new_stop 上移
```

逻辑直接抽自 `scripts/neckline_backtest.py` 的 `simulate_exit`，**零重写**——复用同一离场内核，消除回测/实盘双源真理（呼应 `execution/engine.py` 顶部 `check_exit` 的「双源真理」红线）。

### 6.6 ExecutionEngine 改造（`execution/engine.py`）

- 启动时 `resolve_active()` 一次 → 每个实验 `build_strategy` → 缓存 `{experiment_id: strategy}`；params 不可变故缓存永久有效。若 ExecutionEngine 为常驻 beat 进程，CLI 改实验后需 reload（检测到新 ACTIVE 版本时增量 build 新 experiment_id、丢弃已 ARCHIVED 的）——MVP 若 scan+tick 为短任务则每次启动加载即可，常驻 reload 作为实现期决策
- `tick_pullback`：`load_plans(ARMED)` → 按 `plan.experiment_id` 路由 → `strategy.check_pullback(plan, quote, bars_armed)`（替换当前硬编码 `check_pullback`）；`CANCEL_TIMEOUT` → update_plan(status=CANCELLED)
- `tick_exit`：`load_plans(FILLED)` → `strategy.check_exit(...)`（替换当前 `check_exit` 调用）；`CLOSE_PORTION` → `submit_order(sell, qty=shares×portion)`；`UPDATE_STOP` → update_plan(stop=new_stop)
- **caisen 零行为回归**：`strategies/caisen_pattern.py` 适配器的 `check_exit` 直接调 `caisen.engines.exit_logic.check_exit`，现有 caisen 实盘行为逐字保留；`check_pullback` 复用 engine 既有 `check_pullback` 几何判定（caisen 无 max_wait 撤单）

---

## 7. 运行模式

实验系统**只发 plan，与下单模式无关**。下单模式由 ExecutionEngine 注入的 gateway 决定（现有 `ExecutionExecutor` Protocol + `get_gateway` + `submit_order(dry_run=)`——零新增）：

| 模式 | gateway | 用途 |
|---|---|---|
| **回测 Mock** | `MockExecutionGateway` | 链路验证、回归测试、开发、新实验预演 |
| **miniQMT 虚拟盘** | `QmtExecutionGateway`（连 miniQMT 模拟账户，账号 `10110356`@100万，真实行情虚拟撮合） | 实盘前最后一关、灰度预演 |

**EMT 已废弃，不在支持范围**。`.venv310/Scripts/python` 跑 miniQMT（xtquant 绑 python310）。MVP 两条链路都要跑通端到端。

---

## 8. 错误处理与风控边界（CLAUDE.md 拷问三连）

| 边界 | 处置 |
|---|---|
| **流动性行情** | scan 遇停牌/缺数据：`scan_live` 跳过该标的（颈线法识别内核已含停牌过滤）；下单走限价挂单回踩（不追涨，天然抗滑点） |
| **接口状态机** | `resolve_active` SQLite 读失败 → scan **fail-fast**（无配置不下单）；CLI 改权重写失败 → 事务回滚审计不写；plan 落盘失败 → 整批回滚（不落半批） |
| **资金守恒红线** | `promote`/`set-weight` 校验所有 ACTIVE 权重和 ≤ 1.0，超额拒绝 |
| **策略敞口** | 新实验建议 promote 前 Mock 跑 N 天预演；`rollback` 一键恢复上个 prod；`weight=0` 软下线留审计 |
| **撤单时序**（miniQMT 已知坑） | 盘后撤单柜台不处理（记忆载明 2026-07-21 实测），撤单完整闭环须在交易时段 9:30-15:00；`CANCEL_TIMEOUT` 决策须考虑此约束 |
| **实验熔断** | MVP **不自动 archive**（防误杀），`report` 对连续亏损实验标红告警，人工决策 |

---

## 9. 测试策略（新建 `tests/experiment/`）

- **experiment/ 单元**：`test_models`（状态机合法/非法迁移 + 权重和校验）、`test_store`（SQLite CRUD + 审计 + 事务回滚 + 并发写 WAL）、`test_resolver`（只返 ACTIVE+weight>0 + params 不可变）、`test_cli`（命令端到端）、`test_audit`（changed_fields 旧→新）
- **协议扩展**：`test_neckline_armed_plan`（Signal→plan 字段映射 + shares 按 weight）、`test_neckline_check_exit`（tp1 部分/tp2 全/trailing/超时，复用 simulate_exit 断言）、**`test_caisen_adapter_compat`**（caisen 适配器 check_exit 与老 `exit_logic.check_exit` 逐字一致——回归守护）
- **ExecutionEngine 改造**：`test_engine_routing`（按 plan.experiment_id 路由）、`test_engine_close_portion`（qty=shares×portion）、**`test_engine_caisen_zero_regression`**（caisen plan 走新引擎行为逐字不变）
- **端到端**：`test_e2e_scan_to_order`（create exp → scan_live → ARMED plan(带 exp_id) → tick(Mock) → FILLED → check_exit → CLOSED，exp_id 归因不断链）、`test_e2e_qmt_virtual`（miniQMT 虚拟盘同链路，交易时段跑）、`test_report`（扫 plans 按 exp_id 聚合 prod vs candidate）

---

## 10. 部署与调度

- **scan 触发**：schtasks 每日盘前调 `scan_service.run_scan`（与 daily-brief/sync 同范式）
- **ExecutionEngine beat**：盘中轮询 tick（接记忆「二期自动交易引擎」的 beat，或 schtasks 盘中触发）
- **CLI 改实验**：人工随时，下次 scan 自动生效

---

## 11. 关键决策记录（ADR）

- **ADR1**：在线版本+权重模型（非 prod/candidate 语义）——平台只懂「版本+权重」，消歧最简，支持灰度也支持多策略并存
- **ADR2**：资金比例分流，平台不管持仓归因——A 股券商账户持仓无法物理隔离，归因记账下沉 execution 层，平台保持极简
- **ADR3**：独立 `experiment/` 包 + SQLite，plan 仍 JSON 加归因字段——单一职责、零侵入 storage、风险隔离（storage 全迁 SQLite 独立立项）
- **ADR4**：CLI + 状态机 + 审计 + rollback——与项目 schtasks/CLI 范式一致，低频操作无需 UI，审计是「保障交易质量」刚需
- **ADR5**：出场归策略侧（遵循 `base.py`），扩展 Strategy 协议——不污染 ExecutionEngine，延续 2026-07-20 解耦原则
- **ADR6**：颈线法 `check_exit` 复用 `simulate_exit` 内核——消除回测/实盘双源真理
- **ADR7**：MVP 含颈线法完整接入实盘 scan——一次到位（用户决策），分两步实现（①识别→ARMED plan ②出场状态机适配）
- **ADR8**：运行模式 Mock + miniQMT 虚拟盘（EMT 已废弃）——双链路验收

---

## 12. MVP 验收标准

1. `python -m experiment create/promote/set-weight/archive/rollback/list/report` 全部可用，状态机校验 + 审计正确
2. Mock 链路端到端：create exp → scan_live → ARMED plan(带 exp_id+weight) → ExecutionEngine tick → FILLED → check_exit(tp1部分+tp2全) → CLOSED，归因不断链
3. miniQMT 虚拟盘链路同上跑通（交易时段）
4. caisen 零行为回归：`test_engine_caisen_zero_regression` + `test_caisen_adapter_compat` 全绿
5. `report` 能按 experiment_id 聚合 prod vs candidate 的 PnL/胜率对比
6. 权重和 > 1.0 的 promote/set-weight 被拒；权重变更下次 scan 生效；rollback 一键恢复

---

## 13. 风险与 follow-up

- **ExecutionEngine 改造是 MVP 最大工作量**：tick 编排从「调 caisen 离场」改成「按 exp_id 路由 Strategy」，由 caisen 适配器逐字保留老行为兜底回归
- **miniQMT 撤单时序坑**：盘后撤单不生效，`CANCEL_TIMEOUT` 须在交易时段验证
- **Parameter Lab 一键发布 UI**：follow-up，MVP 手动复制参数到 CLI
- **实时实验看板 / 自动熔断**：follow-up，MVP 仅离线 report
- **`execution/storage.py` 全迁 SQLite**：独立立项，与实验系统解耦
- **caisen 阶段E 完整删除**：follow-up，颈线法稳定运行后启动

链接 [[quanter-industrial-5epics]] [[quanter-backend-layering]] [[neckline-paramiter-baseline]] [[neckline-trailing-stop]]。
