# 实验系统（Experiment System）· 设计文档

> **维护范围**：新增 `experiment/` 包 + 注入 `trading/engine.py::_eod`（二期引擎 gap② 策略数据源）+ `trading/signal_runner.py` 归因透传 + `trading/trading_plan.py` plan 归因字段
> **创建日期**：2026-07-22
> **状态**：v2 · 基于二期引擎修订（v1 误基于旧 `execution/engine.py`，已作废执行侧）
> **修订记录**：
> - **v2（2026-07-22）**：master 合并二期引擎 `trading/` 包（`04f6c1c`）后，发现 v1 §6 + plan Task5-11 基于旧 `execution/engine.py`（盘中 tick 范式）错位。v2 保留配置中心核心（§3-5 + plan Task1-4），重写执行侧对接二期引擎 `trading/engine.py::_eod`（T-1 定计划 + APScheduler 四触发点）。ADR5-7 修正。

---

## 0. 一句话定位

**实验系统是实盘下单的「策略版本配置中心」**，也是二期引擎 `trading/engine.py::_eod` 的「策略数据源注入层」（二期引擎 gap②）：管理「在线实验版本 + 资金权重」，T-1 晚 `_eod` 经 `resolve_active()` 拿到当前线上/灰度的 `(strategy_name, params, weight)` 列表，遍历产信号。线上参数以不可变快照锁定（保障交易质量），灰度以资金权重分流，版本与 plan 归因全持久化。

---

## 1. 背景与动机

### 1.1 触发场景

即将开始 **miniQMT 虚拟盘** 真实下单（EMT 已废弃，见记忆订正）。二期引擎已铺好「T-1 定计划 + 开盘挂单 + 盘中止损监控 + 盘后对账」四触发点骨架，但其 `_eod` 当前 `signals=[]` 占位（`trading/__main__.py` 明确把「策略层数据源注入」留给上线集成阶段）。实盘对「策略及参数稳定性」要求远高于回测——回测里随手改参数重跑无代价，实盘改参数=全市场持仓逻辑剧变。

### 1.2 当前痛点（实盘链路盘点）

- **二期引擎 `_eod` 信号源缺位**（gap②）：`trading/engine.py::_eod` 当前 `signals=[]` 占位 + `TODO(Task 10): 注入 NecklineMethodStrategy`。无配置中心 → 注入即硬编码单策略单参数，手滑风险。
- **`signal_runner` 硬编码单参数集**：`build_orders_from_signals` 签名固定 `capital/pos_cap/stop_cfg`，无实验版本/灰度/权重概念。
- **无版本/灰度/回滚概念**：grep 全仓零 `experiment/gray/feature_flag` 命中。参数一改就覆盖，无审计、无灰度、无逃生通道。
- **颈线法实盘 scan 未接通**：`strategies/neckline_method` 的 `scan_at` 只被回测用，二期引擎 `_eod` 还没调它产信号。

### 1.3 实验系统的职责边界

| 做 | 不做 |
|---|---|
| 管理「在线实验版本 + 资金权重」（不可变快照 + 状态机 + 审计） | 不生成订单、不下单、不动出场（二期 engine/signal_runner/stop_loss 的事） |
| `resolve_active()` 给出当前生效 `(name, params, weight)`；注入 `_eod` 的信号扫描 | 不做盘中出场决策（出场归二期 `stop_loss.compute_stop_price` + 柜台限价止盈） |
| signal/plan 落盘时携带 `experiment_id`+`weight` 归因 | 不记账、不管持仓对账（二期 `reconcile_job` 的事） |
| `report` 事后扫 `logs/trading_plans/plan_*.json` 按 `experiment_id` 聚合 | 不实时算 PnL 指标（离线聚合） |

---

## 2. 目标与非目标

### 2.1 MVP 目标

1. **配置中心**：`experiment/` 独立包，SQLite 持久化（版本 + 审计），CLI 管理（create/promote/set-weight/archive/rollback/list/report）。
2. **二期引擎 `_eod` 注入**：`trading/engine.py::_eod` 从 `signals=[]` 占位 → `resolve_active()` 遍历在线实验 → 每实验 `build_strategy(name, params).scan_at(...)` 产信号（带 `experiment_id`）。
3. **signal_runner 归因 + 权重透传**：`build_orders_from_signals` 改造，资金按 `weight × total_capital` 分配，`PlannedOrder` + `trading_plan` orders 带 `experiment_id`+`experiment_weight`。
4. **双链路验收**：影子模式（AUTO_TRADE_MODE=dry_run）+ miniQMT 虚拟盘都能跑通 T-1 `_eod` → 计划落盘 → 归因不断链。

### 2.2 非目标（MVP 外，follow-up）

- Parameter Lab 冠军参数「一键发布」UI（MVP 用 CLI）
- 实时实验 PnL 看板 / 自动熔断 archive（MVP 仅 `report` 离线聚合）
- 二期引擎 gap①（post_close 熔断 equity 源）/ gap③（EMT 行情源）—— 非实验系统职责
- 盘中分级止盈 tp1/tp2（二期引擎范式是柜台单档限价止盈 + 盘中被动止损，MVP 不引入盘中分级推进）
- caisen 实盘接入（caisen 不在二期实盘路径，仅回测保留）

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

**合法迁移**：DRAFT→ACTIVE(promote)、ACTIVE→ARCHIVED(archive)、ARCHIVED→ACTIVE(rollback)、DRAFT→删除(discard)、ACTIVE 内 set-weight。**非法迁移一律拒绝**。

### 3.4 AuditLog（append-only）

每次 create/promote/set-weight/archive/rollback/discard 写一条：`audit_id / timestamp / action / experiment_id / changed_fields(JSON，如 {"weight":[0.8,0.2]}) / operator / note`。

### 3.5 SQLite Schema（`experiment/experiments.db`）

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

### 3.6 plan 归因字段（落点：`trading/trading_plan.py`）

二期引擎 plan 落 `logs/trading_plans/plan_<date>.json`，结构 `{"date", "confirmed", "orders":[...]}`，每个 order 是嵌套 dict `{"order":{symbol,qty,side,price}, "stop_price", "take_profit"}`。归因 = 给每个 order dict 加两字段：
- `experiment_id: str | None`（pre-experiment 老 plan 为 None）
- `experiment_weight: float | None`（落盘时冻结）

`save_plan` 是 JSON 透传，归因字段由 `signal_runner.build_orders_from_signals` 产出时带上、`eod_plan` 透传。`report` 扫这些 plan 按 `experiment_id` 聚合。

---

## 4. 架构与组件

### 4.1 新增 `experiment/` 包

```
experiment/
  __init__.py     # 导出 resolve_active / 公开 API
  models.py       # ExperimentVersion / AuditLog / ActiveExperiment + ExperimentStatus + 状态机校验
  store.py        # SQLite 持久化（experiments.db），WAL + 事务
  resolver.py     # resolve_active() → [ActiveExperiment]；_eod 唯一入口，实时读不缓存
  audit.py        # 变更审计写入 + 查询
  cli.py          # python -m experiment create|promote|set-weight|archive|rollback|list|report
```

### 4.2 依赖方向（零反向依赖红线）

```
strategies/ ←── build_strategy(name,params) ──← experiment/ (resolver, 只读 SQLite)
   ↑ scan_at(识别内核)                            ↑ 读
trading/engine.py::_eod（改造：signals=[]占位 → resolve 多实验遍历）── resolve_active() ──┘
   │ 每实验 scan_at(params) → signals（带 experiment_id）
   ↓
trading/signal_runner.build_orders_from_signals（改造：weight×capital + 归因透传）
   ↓ PlannedOrder（带 experiment_id + experiment_weight）
trading/trading_plan.save_plan（orders 嵌套 dict 加归因）→ 确认闸
   ↓
trading/engine.py：pre_open（柜台限价挂单）→ stop_loss_monitor（compute_stop_price 盘中监控）→ post_close（reconcile 对账）
   ↓
trading/qmt_gateway.py（miniQMT 虚拟盘）/ MockExecutionGateway（影子/dry_run）
```

**`experiment/` 零依赖** strategies/execution/trading/server —— 纯配置层。`trading/engine.py::_eod` 依赖 experiment（resolve）+ strategies（build_strategy/scan_at）。

### 4.3 注入点（二期引擎 gap②）

`trading/engine.py::_eod`（当前 `signals=[]` 占位）→ 改造调 `resolve_active()` → 遍历在线实验 → 每实验 `build_strategy + scan_at` 产信号 → 合并传 `eod_plan`。

---

## 5. 数据流

### 5.1 T-1 信号扫描（二期引擎 `_eod` 触发点，15:35）

```
trading/engine.py::_eod(date)
  ├─ 1. resolver.resolve_active()                      ← 实时读 SQLite，返 ACTIVE 且 weight>0
  │     → [ActiveExperiment(exp_id, strategy_name, params, weight), ...]
  │     空则 fail-fast（无在线实验，eod_plan 不产单）
  ├─ 2. universe = _load_universe()                    ← 创板科创可交易标的（复用既有）
  ├─ 3. for each ActiveExperiment:
  │     strategy = build_strategy(exp.strategy_name, cfg_override=exp.params)
  │     for sym in universe:
  │       for signal in strategy.scan_at(sym, df.loc[:date], date, state):
  │         signal["experiment_id"] = exp.experiment_id     ← 归因标记
  │         signal["experiment_weight"] = exp.weight
  │         signals.append(signal)
  ├─ 4. eod_plan(date, signals, atr_map, capital)      ← 二期引擎既有，signals 现在非空
  │     → build_orders_from_signals（每实验 weight×capital 算 budget）→ PlannedOrder（带归因）
  │     → trading_plan.save_plan（orders 嵌套 dict + 归因）→ push 钉钉等确认
  └─ 5. [人工钉钉确认] → pre_open(09:22) 挂限价买 + 止盈限价卖
        → stop_loss_monitor(盘中每5min) 跌破 stop_loss.compute_stop_price → 发卖
        → post_close(15:30) reconcile 对账 + 重算次日止损
```

### 5.2 CLI 操作流

```bash
python -m experiment create  --strategy neckline --params '{...}' --source "param_lab:run_xxx"
python -m experiment promote <exp_id> --weight 0.2          # DRAFT→ACTIVE，校验权重和≤1.0
python -m experiment set-weight <exp_id> --weight 0.5       # 灰度扩量（记审计）
python -m experiment archive <old_prod_id>                  # 下线
python -m experiment rollback <archived_id>                 # 回滚（ARCHIVED→ACTIVE）
python -m experiment list [--status active]
python -m experiment report --since 2026-07-01              # 扫 trading_plans/plan_*.json 按 exp_id 聚合
```

### 5.3 权重热生效（零常驻进程一致性问题）

`resolve_active()` 每次 `_eod`（每日 15:35 触发）**实时读 SQLite 不缓存**。CLI 改权重 → 写 SQLite+审计 → **次日 `_eod` 自动生效**。二期引擎是 APScheduler 常驻进程，但 `_eod` 每次触发都重读 SQLite，故无需 reload 机制。

### 5.4 一致性边界

- **权重变更 vs in-flight plan**：plan 落盘时 `experiment_weight` **冻结**。CLI 改权重只影响之后 `_eod` 新产的 plan；已挂单/持仓按落盘时权重执行。
- **多实验同标的**：prod/candidate 都扫到标的 S → 各产独立 signal（不同 exp_id）→ `signal_runner` 各自算 qty（weight×capital）→ 两笔独立 PlannedOrder。持仓合并是券商账户层的事（二期 `reconcile` 对账，平台不管）。
- **weight=0**：resolve 过滤 `weight>0`，权重调 0 = 软下线。

---

## 6. 实盘接入二期引擎（v2 重写：对接 `trading/engine.py::_eod`）

### 6.1 注入点：二期引擎 `_eod` 的 gap②

二期引擎 `trading/__main__.py` 顶部明确：「**本入口【不】做策略层数据源注入**……策略层→引擎层信号源集成 = 二期引擎上线集成阶段」。四触发点 `_eod/_pre_open/_stoploss/_post_close` 当前是安全 no-op（数据源空时优雅降级）。实验系统的核心职责 = **填上 `_eod` 的信号源注入**，把 `_eod` 从 `signals=[]` 占位变成 resolve 多实验产信号。

### 6.2 `trading/engine.py::_eod` 改造（替换占位）

```python
# 改造前（当前）：
async def _eod(self):
    # TODO(Task 10): 注入 NecklineMethodStrategy + 拉 universe → signals + atr_map
    await eod_plan(today, signals=[], atr_map={}, capital=...)

# 改造后：
async def _eod(self):
    today = datetime.now().strftime("%Y-%m-%d")
    if not calendar.is_trading_day(today):
        return
    from experiment.resolver import resolve_active
    from strategies.registry import build_strategy
    experiments = resolve_active()
    if not experiments:
        logger.warning("_eod 无在线实验，跳过"); return   # fail-fast
    universe = _load_universe()                           # 创板科创可交易标的
    signals, atr_map = [], {}
    for exp in experiments:
        strategy = build_strategy(exp.strategy_name, cfg_override=exp.params)
        for sym in universe:
            df = _load_df_upto(sym, today)                # 无前视 .loc[:today]
            for s in strategy.scan_at(sym, df, today, {}):
                s["experiment_id"] = exp.experiment_id    # 归因标记
                s["experiment_weight"] = exp.weight
                signals.append(s)
                atr_map[sym] = strategy.atr(sym)          # ATR 供 stop_loss 用
    await eod_plan(today, signals, atr_map, capital=float(os.getenv("TRADE_CAPITAL","1_000_000")))
```

**实现说明**：`_load_universe`/`_load_df_upto`/`strategy.atr` 的具体来源对齐 `strategies/neckline_method.py` 既有 `scan_at` 的数据加载方式（该文件 `scan_at` 已实现识别内核 + 数据加载，此处抽出复用）。

### 6.3 `trading/signal_runner.py` 改造（资金权重 + 归因透传）

`build_orders_from_signals` 当前用单一 `capital × pos_cap`。改造：signal 带 `experiment_weight`，budget = `experiment_weight × capital × pos_cap`；PlannedOrder 加 `experiment_id`/`experiment_weight`：

```python
@dataclass
class PlannedOrder:
    order: OrderRequest
    stop_price: float
    take_profit: float
    neckline: float
    experiment_id: str = ""        # 新增：归因
    experiment_weight: float = 1.0 # 新增：冻结权重

def build_orders_from_signals(signals, *, capital, pos_cap, atr_map, stop_cfg):
    for s in signals:
        weight = s.get("experiment_weight", 1.0)         # 每信号各自的权重
        budget = capital * pos_cap * weight              # weight 落地
        qty = int(budget / float(entry) / 100) * 100
        ...
        out.append(PlannedOrder(..., experiment_id=s.get("experiment_id",""),
                                experiment_weight=weight))
```

`eod_plan` 产 order_dicts 时把 `experiment_id`/`experiment_weight` 透传到嵌套 dict，`trading_plan.save_plan` JSON 透传落盘。

### 6.4 出场：复用二期引擎既有（不另造 check_exit）

颈线法出场在二期引擎已定型，实验系统**不动出场逻辑**：

| 出场事件 | 二期引擎既有实现 | 实验系统 |
|---|---|---|
| **止盈** | `trading/engine.py::pre_open` 挂柜台限价卖单（`tp_h_mult × H`，T 日 09:22 开盘前挂） | 不动 |
| **止损** | `trading/stop_loss.py::compute_stop_price`（trailing grace/step/floor，**已从 `simulate_exit` 迁出**，T-1 重算固定价，盘中 `stop_loss_monitor` 监控） | 不动 |
| **盘中分级 tp1/tp2** | 二期引擎**无此范式**（柜台单档限价止盈 + 盘中被动止损） | MVP 不引入 |

**v1 的 `check_exit`/`check_pullback`/tp1-tp2 分级推进 / `_exit_kernel` 全部作废**——二期 `stop_loss.compute_stop_price` 已经把 trailing 做掉了，颈线法出场范式是 T-1 定计划 + 柜台挂单，不是盘中 tick 推进。

### 6.5 plan 归因落点：`trading/trading_plan.py` orders 嵌套 dict

每个 order dict 加归因：
```json
{"order": {"symbol":"...", "qty":..., "side":"buy", "price":...},
 "stop_price": ..., "take_profit": ...,
 "experiment_id": "neckline_v6_20260722", "experiment_weight": 0.2}
```
`save_plan` JSON 透传（既有逻辑不改，归因字段由 signal_runner 产出时带）。`report` 扫 `logs/trading_plans/plan_*.json` 按 `experiment_id` 聚合。

### 6.6 作废清单（v1 → v2）

| v1 设计（基于旧 execution/engine.py） | v2 处置 |
|---|---|
| §6.2 Strategy 协议加 `scan_live/to_armed_plan/check_pullback/check_exit` | **作废**——二期用 `scan_at` + signal_runner，不需新协议方法 |
| §6.5 颈线法 `check_exit`（`_exit_kernel` 复用 simulate_exit） | **作废**——二期 `stop_loss.compute_stop_price` 已迁出 trailing |
| §6.6 `execution/engine.py` ExecutionEngine 改造（tick_pullback/tick_exit 路由） | **作废**——`execution/engine.py` 不在实盘路径，实盘走 `trading/engine.py` |
| caisen 适配器 `check_exit` + 零回归守护 | **作废**——caisen 不在二期实盘路径 |

---

## 7. 运行模式（协同二期引擎 AUTO_TRADE_MODE 影子闸）

实验系统**只产信号 plan，与下单模式无关**。下单由二期引擎 `_pre_open/_stoploss/_post_close` 执行，模式由 `AUTO_TRADE_MODE` env 决定（二期既有）：

| 模式 | gateway / 闸 | 用途 |
|---|---|---|
| **影子 dry_run** | `AUTO_TRADE_MODE=dry_run`（默认）+ MockExecutionGateway | 链路验证、新实验预演、回归测试 |
| **miniQMT 虚拟盘** | `AUTO_TRADE_MODE=live` + QmtExecutionGateway（账号 `10110356`@100万模拟） | 实盘前最后一关、灰度预演 |

**EMT 已废弃**，不在支持范围。`.venv310/Scripts/python` 跑 miniQMT（xtquant 绑 python310）。

⚠️ **二期引擎 live 禁切红线**：当前 live 模式待 3 必修（post_close 熔断 equity 源 / 策略数据源注入 / EMT 行情源）。实验系统完成 §6 注入 = 解锁必修②（策略数据源注入），但①③仍需二期引擎补。MVP 验收用 dry_run 跑通，miniQMT 虚拟盘作为 live 前置验证（待二期 gap①③ 补全后再切 live）。

---

## 8. 错误处理与风控边界（CLAUDE.md 拷问三连）

| 边界 | 处置 |
|---|---|
| **流动性行情** | `_eod` scan 遇停牌/缺数据：`scan_at` 内部跳过；下单走柜台限价（pre_open 挂颈线+ATR，不追涨） |
| **接口状态机** | `resolve_active` SQLite 读失败 → `_eod` fail-fast（无配置不产单）；CLI 改权重写失败 → 事务回滚审计不写 |
| **资金守恒红线** | `promote`/`set-weight` 校验所有 ACTIVE 权重和 ≤ 1.0，超额拒绝 |
| **策略敞口** | 新实验建议 promote 前 dry_run 跑 N 天；`rollback` 一键恢复；`weight=0` 软下线留审计 |
| **撤单时序**（miniQMT 已知坑） | 盘后撤单柜台不处理（记忆 2026-07-21 实测），二期 `pre_open` 撤昨日未成交须在交易时段 |
| **实验熔断** | MVP 不自动 archive，`report` 对连续亏损实验标红告警，人工决策 |

---

## 9. 测试策略（新建 `tests/experiment/` + 改造 `tests/trading/`）

- **experiment/ 单元**（v1 保留）：`test_models`（状态机 + 权重和）、`test_store`（SQLite CRUD + 审计 + 事务）、`test_resolver`（只返 ACTIVE+weight>0）、`test_cli`（命令端到端）、`test_audit`
- **_eod 注入测试**（v2 新）：`test_eod_resolves_experiments`（resolve → 每实验 scan_at → signals 带 exp_id）、`test_eod_failfast_no_active`（无在线实验不产单）
- **signal_runner 归因测试**（v2 新）：`test_build_orders_weight_budget`（qty = weight×capital×pos_cap）、`test_planned_order_carries_experiment_id`
- **trading_plan 归因测试**（v2 新）：`test_save_plan_preserves_experiment_attribution`（orders 嵌套 dict 带 exp_id 往返）、`test_old_plan_without_attribution`（向后兼容 None）
- **端到端**（v2 改）：`test_e2e_eod_to_plan`（create exp → `_eod` resolve → scan_at → PlannedOrder(带 exp_id) → trading_plan 落盘 → 归因不断链，全程 dry_run）
- **report**（v1 保留，落点改）：扫 `logs/trading_plans/plan_*.json` 按 exp_id 聚合

**作废测试**（v1 的，不写）：颈线法 check_exit/tp1tp2 分级、caisen 适配器零回归、ExecutionEngine tick 路由。

---

## 10. 部署与调度

- **二期引擎常驻**：`python -m trading`（APScheduler 四 cron，`run_trading_engine.bat` schtasks 开机自启）
- **`_eod` 触发**：`ENGINE_EOD_PLAN_CRON=35 15 * * 1-5`（T-1 晚 15:35），交易日判定由 `calendar.is_trading_day`
- **CLI 改实验**：人工随时，次日 `_eod` 自动生效

---

## 11. 关键决策记录（ADR）

- **ADR1**：在线版本+权重模型（非 prod/candidate 语义）——平台只懂「版本+权重」
- **ADR2**：资金比例分流，平台不管持仓归因——A 股券商账户持仓无法物理隔离，归因记账下沉二期 reconcile
- **ADR3**：独立 `experiment/` 包 + SQLite，plan 归因落 `trading_plan` orders（不动 execution/storage）
- **ADR4**：CLI + 状态机 + 审计 + rollback
- **ADR5（v2 修正）**：实验系统注入 `trading/engine.py::_eod`（二期引擎 gap② 策略数据源），**不改 `execution/engine.py`**（v1 误基于旧引擎，已作废）
- **ADR6（v2 修正）**：出场复用二期 `stop_loss.compute_stop_price`（trailing 已迁出）+ 柜台限价止盈，**不另造 `check_exit`/`_exit_kernel`**（v1 与二期 stop_loss 重复，已作废）
- **ADR7（v2 修正）**：MVP = 配置中心（Task1-4）+ 注入 `_eod`/signal_runner 归因（对接二期引擎），不碰盘中出场
- **ADR8**：运行模式协同二期 `AUTO_TRADE_MODE`（dry_run 影子 + miniQMT 虚拟盘），EMT 已废弃

---

## 12. MVP 验收标准（v2）

1. `python -m experiment create/promote/set-weight/archive/rollback/list/report` 全可用，状态机 + 审计正确
2. `_eod` 注入端到端（dry_run）：create exp → `_eod` resolve → 每实验 `scan_at(params)` → signals(带 exp_id) → `signal_runner` 产 PlannedOrder(weight×capital) → `trading_plan` 落盘(orders 带 exp_id) → 归因不断链
3. miniQMT 虚拟盘链路同上跑通（待二期 gap①③ 补全后切 live 验证；MVP 用 dry_run + miniQMT 模拟账户 pre_open 挂单验证）
4. `report` 按 `experiment_id` 聚合 prod vs candidate 的 PnL/胜率对比（扫 `trading_plans/plan_*.json`）
5. 权重和 > 1.0 的 promote/set-weight 被拒；权重变更次日 `_eod` 生效；rollback 一键恢复
6. 二期引擎既有 81 测试零回归（实验系统只注入 `_eod`，不动 pre_open/stop_loss/post_close）

**v1 验收作废项**：caisen 零行为回归（caisen 不在实盘路径）、盘中 check_exit tp1/tp2 分级。

---

## 13. 风险与 follow-up

- **二期引擎 gap①③ 未补**：post_close 熔断 equity 源 + EMT 行情源。实验系统只解 gap②，miniQMT live 验收需待①③补全。
- **scan_at 实盘数据加载**：`_load_universe`/`_load_df_upto` 需对齐颈线法既有加载（创板科创 universe + 前复权日线 .loc[:today] 无前视）
- **多实验同标的 qty 叠加**：prod+candidate 同标的两笔 PlannedOrder，券商账户合并持仓——二期 reconcile 对账按总量，实验归因按 plan_id 事后拆（平台不管实时拆账）
- **Parameter Lab 一键发布 UI**：follow-up
- **实时实验看板 / 自动熔断**：follow-up
- **盘中分级止盈 tp1/tp2**：二期引擎当前单档柜台止盈，若需 tp1 部分止盈要二期引擎扩 pre_open 挂单逻辑（非实验系统职责）

链接 [[quanter-ops-layer-phase1]] [[quanter-industrial-5epics]] [[quanter-backend-layering]] [[neckline-paramiter-baseline]]。
