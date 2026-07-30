# 调度编排层设计（Scheduling Orchestration，方向 B 第 2 条路，解决 C-2）

> 日期：2026-07-30 ｜ 状态：待 review ｜ 作者：AI 研究员 + 用户（brainstorming skill 重新生成）
> 关联：[[2026-07-29-trading-execution-resilience-design]]（韧性层，与本设计正交）、[[plan-sqlite-deferred]]、[[eod-date-offbyone-fix]]
> 源 session：`sess_852452fb`（C-2 调度编排层方案，msg 1/4/8/11/12/15/19 决策链）

> ⚠️ **本文件取代旧轮询版**。旧版（"事件通道 + 60s 轮询订阅 + 不合并调度器"）是源 session
> msg 8 已被用户**否决**的前版。msg 15 的最终决策是**合并到 uvicorn 单进程**——本 spec 反映该决策。
> 用 brainstorming skill 于 2026-07-31 重新生成（旧版简略且含 5 处事实错误，见 §1.5）。

## 0. 决策摘要（用户已拍板）

| # | 决策点 | 选择 |
|---|---|---|
| D1 | 架构方向 | **合并调度器到 `uvicorn presentation.server.main:app` 单进程**（方向 B 第 2 条路），用事件链取代"19:00 时钟赌博"。**否决**轮询订阅、**否决**Kafka、**否决**"不合并只加轮询" |
| D2 | 数据就绪范围 | **校验本次策略计算依赖的数据集**（动态，非硬编码 daily）。data_ready 表支持多 key |
| D3 | 数据依赖声明 | **策略声明 `required_data_keys`**（Strategy Protocol 加属性，默认 `{"daily"}`）|
| D4 | 就绪信号落库点 | **在 T2 步骤（`run_data_check.py`）内就地落库**——结构化结果零丢失，supervisor 不动 |
| D5 | 事件链编排位置 | **`trading/orchestrate/pipeline.py`**（新文件，编排层，高内聚低耦合）|
| D6 | spec 落盘 | **原地重写本文件**（取代旧轮询版）|

---

## 1. 背景（事实，非推测）

### 1.1 三套调度器并行，无统一编排

| 轨 | 技术 | 位置 | 管 |
|---|---|---|---|
| 实时交易 | APScheduler `AsyncIOScheduler`（engine 进程内） | `trading/engine.py:1557-1655`（`TradingEngine.__init__`） | eod@19:00 / pre_open@09:22 / stoploss@30s / post_close@15:30 / health_guard@60s |
| 数据/观测 | Windows `schtasks`（系统任务计划，独立进程） | `ops/manage_ops_schtasks.py:28-30`、`discovery/schtasks.py:28-33` | data_pipeline@17:00 / brief@18:00 / discovery@02:00 |
| 开机自启 | `start_all.bat` → `ops/start_all.py` + Startup 快捷方式 | `ops/start_all.py:83-146` | 把上面两轨拉起并注册 |

### 1.2 数据采集 → eod 的信号链是断的（C-2 真正病灶）

采集进程（`ops/data_pipeline.py`，schtasks 触发）与 eod 进程（engine 内 APScheduler）是**两个独立进程，跨进程零 IPC，只共享文件系统**。现状靠**固定时间差赌博**：

- `data_pipeline.py` 串行跑 T1→采集→T2（`ops/data_pipeline.py:34-38`）；T2 步骤 `run_data_check.py` 的 `run_check()` 返回结构化 dict `{"ok","melted","details"}`（`run_data_check.py:95-135`），但 supervisor 用 `subprocess.call` 只拿到退出码，结构化细节丢失。
- eod@19:00 直接 `pd.read_parquet(...)`（`engine.py:1849`），**不校验数据是否真采到**。注释（`engine.py:1593-1599`、`1805-1809`）自记：原本 15:35 触发会读到 T-1 旧数据算 T+1 计划（时序 bug），"修复"方式只是把 cron 挪到 19:00"等足"。采集超时/失败时 19:00 eod 照常跑、产废信号。
- 现成的 `check_freshness(key, expected_date)`（`data/freshness.py:39`）已能真正回答"T 日数据到没到"，但**只在采集进程 T2 步骤里跑，结果没落库、eod 订阅不到**。

### 1.3 pre_open 只有"计划确认"一道显式 gate

`pre_open`（`engine.py:411`）第一道硬 gate 是 `plan["confirmed"]`（`engine.py:503-526`），但**不显式检查网关健康、不显式检查数据就绪**——网关健康靠"进程启动 connect + 常驻 health_guard"（`engine.py:1657`）隐式保障，数据就绪靠"eod 排 19:00 等足"隐式保障。无任何"全绿才挂单"的显式前置 gate。

### 1.4 策略数据依赖是隐式硬编码（C-2 的隐藏债）

`_eod` 硬编码只读 `a_shares_daily.parquet`（`engine.py:1849`）；`Strategy` Protocol（`strategies/base.py:46`）**没有任何"我依赖哪些数据集"的声明**，依赖硬编码在策略实现里（`strategies/neckline/method_v0.py:394`）。这导致无法校验"本次策略实际需要的数据集是否就绪"。

### 1.5 旧轮询版的 5 处事实错误（本 spec 已修正）

1. **import 路径错**：旧版写 `from calendar_utils import expected_latest_trade_day`——实际在 `trading/calendar.py`，`run_data_check.py:18` 已正确 import。
2. **melted 推断错**：旧版说"从 T2 rc==2 推断 melted"——但 `run_check()` 已返回结构化 dict，靠退出码反推多余且脆。
3. **就绪定义静态化**：旧版默认 `dataset="daily"` 硬编码，与"校验本次策略数据集"意图冲突。
4. **方案被否决却写成定稿**：旧版"轮询订阅不合并"是源 session msg 8 已否决的前版。
5. **未评估 T2 重复 read_parquet**：旧版在 supervisor 末尾"再跑一遍"会重复 1.75s×N I/O。

---

## 2. 目标 / 非目标

**目标**
- **O1 信号链事件化**：把"采集完成→eod"从"19:00 时钟赌博"改成同进程内 `await proc.wait()` 确定性等待，零 IPC、零时间差赌博。
- **O2 数据就绪声明式**：策略声明 `required_data_keys`，eod/gate 据此动态校验"本次策略实际需要的数据集"。
- **O3 pre_open 三段式显式 gate**：计划确认 + 网关健康 + 数据就绪，全绿才挂单。
- **O4 可观测**：就绪/未就绪/熔断事件落库 + 钉钉（CRITICAL），取代当前"进程退出码 + 事后发现"。

**非目标（YAGNI，本次不做）**
- 不引入 Airflow / Prefect / Celery / Redis / Kafka 等重型框架/中间件（违背韧性 spec §2"不引入新依赖"）。
- 不改交易日内四阶段的业务算法（eod/pre_open/post_close 算法不动，只改触发与 gate 机制）。
- 不改数据采集算法（`sync_daily_incremental` 不动，只在 T2 末尾"接线"写就绪信号）。
- 不改 plan 存储（仍 JSON 文件 `logs/trading_plans/plan_<date>.json`，[[plan-sqlite-deferred]] 独立任务）。
- 不改 discovery 轨（discovery@02:00 schtasks 保留，与本设计正交）。

---

## 3. 总体架构：合并到 uvicorn 单进程 + 事件链编排

把交易轨（engine APScheduler）并入 `presentation/server/main:app` 的 lifespan 装配块；数据采集从独立 schtasks 进程改成 engine 内 `_pipeline_then_eod` job 的子进程。采集与 eod 在**同一进程内**串成事件链，`await proc.wait()` 确定性等采集完成，再 `check_freshness` 验证内容——零 IPC、零时间差赌博。

```
┌──────────────── uvicorn 进程（presentation.server.main:app）────────────────┐
│                                                                              │
│  lifespan 装配块（已有模式：notifier/lake/replay_scheduler/training/...）     │
│  【新增装配块】TradingEngine + pipeline_then_eod job  ── try/except 不阻断    │
│                                                                              │
│    engine = TradingEngine()          ← W3: 7步初始化收口进 bootstrap()        │
│    await engine.bootstrap()          ← connect + DB init（构造器零 I/O）       │
│    if check_shadow_gate():           ← W2: sys.exit → 返 bool                 │
│        engine.start()                ← 注册 cron（含新 _pipeline_then_eod）   │
│                                                                              │
│    _pipeline_then_eod job（盘后触发，取代独立 schtasks 采集进程）            │
│      位置：trading/orchestrate/pipeline.py（D5，编排层）                      │
│      proc = await create_subprocess_exec(python, ops/data_pipeline.py)       │
│      await proc.wait()                ← 采集串行完成（T1→采→T2）              │
│      keys = Σ strat.required_data_keys（D3，本次实验策略声明的依赖并集）      │
│      results = {k: check_freshness(k, expected)}  ← 复用纯函数               │
│      if all ok: upsert_data_ready + await engine._eod()                      │
│      else:     notify CRITICAL，不产废信号                                    │
│                                                                              │
│  pre_open@09:22（cron 不变）                                                 │
│    【新增】_pre_open_gate：①计划确认 ②网关健康 ③数据就绪 → 全绿才挂单        │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

**为什么这是"真事件驱动"而非旧 spec 的伪事件**：采集完成这个事件不再是"19:00 时钟到了我猜采集该完了"，而是 `await proc.wait()` **确定性地等到采集进程退出**，再 `check_freshness` 验证内容，**同进程串行，零 IPC，零时间差赌博**。

---

## 4. 文件结构与职责分层（高内聚低耦合）

### 4.1 遵从既有五层架构（Layer2，已核实）

本设计**不发明新分层**，严格遵从代码库已有的 functional core / imperative shell / orchestrate 分层：

```
trading/
├── compute/      functional core  — 纯判定（止损/风控/对账），零 I/O，回测实盘共用
│   └── risk.py       check_order（纯函数，已接受 whitelist 参数）
├── io/           imperative shell — 副作用壳（下单/撤单/查仓/查价），只搬运不判定
├── orchestrate/  编排层 — 连线 compute+io，自身无业务判定（铁律：只连线不判定）
├── engine.py     APScheduler 容器 — cron 注册 + start/shutdown（留根，因测试 monkeypatch）
├── dynamic_whitelist.py  W1 红线：模块级 _DYNAMIC 全局
└── state_store.py        交易状态库（6→7 张表）
```

**层间铁律（已写入各子包 docstring）**：`compute` 零 I/O；`io` 只搬运不判定；`orchestrate` 只连线不判定；`engine.py` 是 cron 容器。

### 4.2 C-2 每块落到哪一层（按逻辑性质归位）

| C-2 工作单元 | 性质 | 落点 | 理由（对齐契约） |
|---|---|---|---|
| `_pipeline_then_eod`（采集→wait→freshness→eod 编排） | 编排（连线子进程+检查+eod） | `trading/orchestrate/pipeline.py`（新） | 连线非判定非 I/O。经 `orchestrate` 门面暴露，注册成 engine cron |
| `check_freshness` 校验 | 纯判定 | `data/freshness.py`（已存在，不动） | 已有归属 |
| `data_ready` 表 + CRUD | 状态持久化 | `trading/state_store.py` | 复用现有 `_connect`/幂等范式，第 7 张表 |
| T2 `run_check()` 落库 | 采集侧副作用 | `data/tools/run_data_check.py` 内改 | 算完结构化结果就地 upsert（D4，内聚） |
| W1 `_DYNAMIC`→实例属性 | 状态归属重构 | `dynamic_whitelist.py` + `engine.py` | 见 §5.1 |
| W2 `sys.exit`→bool | 降级而非杀进程 | `__main__.py` | 见 §5.2 |
| W3 7步初始化封装 | 构造器收口 | `engine.py TradingEngine` | 见 §5.3 |
| 策略 `required_data_keys` | 契约扩展 | `strategies/base.py` Protocol | 见 §6.1 |
| pre_open 三段 gate | 编排门控 | `engine.py _pre_open_gate` | 节奏性跳过（非业务判定）归编排 |

### 4.3 新增/改动文件清单（精确到文件）

**新增（1 个文件）：**
```
trading/orchestrate/pipeline.py   ← pipeline_then_eod 事件链编排（async）
```

**改动（7 个文件，每个改动单一职责）：**
```
presentation/server/main.py      ← lifespan 加 engine 装配块（仿 replay_scheduler 范式）
trading/engine.py                ← W1 实例属性 + W3 bootstrap() + _pipeline_then_eod cron 注册 + _pre_open_gate
trading/dynamic_whitelist.py     ← W1: 保留模块全局（server 用）+ 提供 static_env_whitelist() 供 engine 拼
trading/__main__.py              ← W2: sys.exit(2) → check_shadow_gate() 返 bool
data/tools/run_data_check.py     ← T2 run_check() 末尾 upsert_data_ready（D4）
trading/state_store.py           ← 第 7 张表 data_ready + upsert/get_data_ready
strategies/base.py               ← Protocol 加 required_data_keys（默认 {"daily"}）
```

### 4.4 为什么这样切分是高内聚低耦合

**内聚**：每块改动落在它性质所属的层——编排归 orchestrate，状态归 state_store，采集落库归 run_data_check。无跨层泄漏（不把采集逻辑塞 engine、不把 freshness 判定搬 server）。

**耦合**：
- `orchestrate/pipeline.py` 只依赖 `data.freshness`（纯函数）+ `subprocess`（标准库）+ engine 的 `_eod`——**单向，不反向依赖 server**。
- server lifespan 装配 engine 是**已有的寄生模式**（replay_scheduler/training_orchestrator 都这样），不新增耦合类型。
- W1 白名单从"模块全局污染"变成"实例属性参数透传"——`check_order`（纯函数）已接受 whitelist 参数，改完后 server 和 engine 各自传各自的，**物理隔离**。

---

## 5. 工程整合项 W1/W2/W3

### 5.1 W1 · 白名单前视污染（最关键）

**当前耦合点（追到根）**：`engine` 和 `server` 共享同一个 `trading_service.submit_order`（`trading_service.py:446`），它内部调 `_whitelist()`（`:468`）→ `get_effective_whitelist()` → 读**模块级全局 `_DYNAMIC`**（`dynamic_whitelist.py:24`）。现在隔离靠"两个进程"；合并后 engine 在 pre_open 注入的白名单会泄漏到 server 手动下单路径 = 前视污染。

**解法：模块全局 → 实例属性 + 显式参数透传（单点收敛）**

`check_order` 已是纯函数、已接受 `whitelist` 参数（`compute/risk.py:55`）——瓶颈只在 `submit_order` 怎么拿 whitelist：

```python
# trading_service.py —— submit_order 加可选 whitelist 参数
async def submit_order(order, *, dry_run, confirm, whitelist: set | None = None) -> dict:
    decision = check_order(order, ..., whitelist=whitelist or _whitelist(), ...)
```

```python
# engine.py —— _submit 注入 engine 实例白名单（不再走模块全局）
async def _submit(order, *, confirm=True, whitelist=None) -> dict:
    from presentation.server.services.trading_service import submit_order as svc_submit
    return await svc_submit(order, dry_run=(_mode()=="dry_run"), confirm=confirm,
                            whitelist=whitelist or (self._dynamic_whitelist | static_env_whitelist()))
```

- `engine.py:652` `inject_dynamic_whitelist(symbols)` → `self._dynamic_whitelist |= symbols`
- `engine.py:1544` `clear_dynamic_whitelist()` → `self._dynamic_whitelist.clear()`
- `TradingEngine.__init__` 初始化 `self._dynamic_whitelist: set[str] = set()`
- `dynamic_whitelist.py` 的 `get_effective_whitelist()` **保留**——server 手动下单路径继续用它（读纯 env，`_DYNAMIC` 模块全局留空 = 向后兼容红线）。

**为什么低耦合**：server 路径不传 `whitelist` → 走 `_whitelist()` = 纯 env（行为不变）；engine 路径显式传 `self._dynamic_whitelist` → 物理隔离。两条路径同一个函数，**靠参数而非全局状态区分**。

### 5.2 W2 · `_shadow_gate` 的 `sys.exit(2)` 杀整个 uvicorn

**当前**：`__main__.py` 的 `_shadow_gate()` 影子期不足时 `sys.exit(2)`——独立进程下没问题，合并后会让整个 API server 退出。

**解法：改返 bool，lifespan 决定是否起 scheduler（API 照运行）**

```python
# 抽成可复用函数（从 __main__ 搬出，改名 check_shadow_gate，返 bool）
def check_shadow_gate() -> bool:
    """影子期不足返 False（原 sys.exit(2) → return False）。"""
    ...

# presentation/server/main.py lifespan engine 装配块：
if check_shadow_gate():
    engine.start()
else:
    logger.warning("影子期不足，scheduler 不启动，API 继续运行")
    fire_and_forget(notify_risk_event("影子期不足，scheduler 未启动", "CRITICAL"))
    # engine 不 start()，但 app 仍可服务（手动下单/查询不受影响）
```

**降级语义**：拒绝只影响"自动交易 scheduler"，API server 必须继续运行（原 sys.exit 是因为独立进程退了无所谓）。

### 5.3 W3 · 7 步连接/DB 初始化封装进 `TradingEngine`

**当前**：`__main__._run_forever()` 裸做 7 步（connect / set_callback / position_book.init_db / state_store.init_store / _migrate / start / loop）。

**解法：封装成 `TradingEngine.bootstrap()` async 方法，三段清晰**

```python
class TradingEngine:
    def __init__(self) -> None:
        """构造：零 I/O，只装配 scheduler + 注册 cron job（已有）。"""
        self._dynamic_whitelist: set[str] = set()   # W1
        ...（现有 cron 注册不动）

    async def bootstrap(self) -> None:
        """W3：I/O 初始化收口（原 __main__._run_forever 的 7 步）。"""
        gw = get_gateway()
        if gw is not None:
            await gw.connect()
            gw.set_order_update_callback(self._handle_order_update)
            self._gw = gw
        from trading import position_book, state_store
        position_book.init_db()
        state_store.init_store()
        state_store._migrate_env_to_account()

    def start(self) -> None:
        """调度启动（已有）。"""
        self.sched.start()
```

三段：**构造（零 I/O）→ bootstrap（I/O 初始化）→ start（调度启动）**。

---

## 6. 数据依赖声明 + eod/pre_open gate

### 6.1 策略 `required_data_keys`（`strategies/base.py`，D3）

当前 `Strategy` Protocol 有 `precompute`/`scan_at`/`config_schema`，无数据依赖声明。加一个属性：

```python
@runtime_checkable
class Strategy(Protocol):
    ...
    @property
    def required_data_keys(self) -> frozenset[str]:
        """本策略依赖的数据集 registry key（如 {"daily"}）。

        eod/gate 据此决定要校验/读取哪些数据集。默认 daily；
        子类可覆盖声明额外依赖（如 moneyflow/margin）。
        """
        ...
```

`NecklineMethodStrategy`（`strategy.py:61`）当前只读 daily，**不显式覆盖**即继承默认 `{"daily"}`——零改动。未来某策略要用资金流，加一行 `required_data_keys = frozenset({"daily","moneyflow"})`，gate 自动跟上，**无需改 engine/gate 代码**——扩展点收敛到策略类一处。

### 6.2 `pipeline_then_eod` 事件链（`trading/orchestrate/pipeline.py`，D5）

```python
async def pipeline_then_eod(engine) -> None:
    """C-2 事件链：采集 → 等完成 → 按策略声明校验数据 → eod。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if not calendar.is_trading_day(today):
        return
    # 1. 采集子进程（原 ops/data_pipeline.py，T1→采→T2）
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "ops/data_pipeline.py", cwd=ROOT)
    rc = await proc.wait()
    # 2. 装配本次实验策略 → 收集依赖 key 并集（D3）
    experiments = resolve_active()
    keys: set[str] = set()
    for exp in experiments:
        strat = build_strategy(exp.strategy_name, exp.params)
        keys |= strat.required_data_keys
    # 3. 按声明的 key 逐个校验（复用 check_freshness 纯函数，不读旧 parquet）
    expected = expected_latest_trade_day(datetime.now())
    results = {k: check_freshness(k, expected) for k in keys}
    all_ok = all(r.ok for r in results.values())
    # 4. 落就绪事件（供 pre_open 防御性双检）
    for k, r in results.items():
        upsert_data_ready(today, k, ok=r.ok, melted=(not all_ok and rc != 0),
                          latest_date=r.latest_date, expected_date=expected,
                          message=r.message)
    if not all_ok:
        msg = f"数据未就绪：{[r.message for r in results.values() if not r.ok]}，eod 跳过"
        fire_and_forget(notify_risk_event(msg, "CRITICAL"))
        return                              # 不跑 eod，不产废信号
    # 5. 全绿 → 跑 eod
    await engine._eod()
```

**说明**：T2 步骤（`run_data_check.py`）内部仍就地落库（D4），与本函数写同一张 `data_ready` 表。本函数这层落库是给"非 supervisor 触发路径"兜底，两者 upsert 幂等不冲突。

### 6.3 `data_ready` 表（`trading/state_store.py`，第 7 张表）

```sql
CREATE TABLE IF NOT EXISTS data_ready (
    date          TEXT NOT NULL,        -- T（YYYY-MM-DD，交易日）
    dataset       TEXT NOT NULL,        -- registry 语义 key（如 "daily"）
    ok            INTEGER NOT NULL,     -- 1=就绪（latest>=expected）；0=缺失/陈旧
    melted        INTEGER NOT NULL DEFAULT 0,  -- 1=超 deadline 仍 FAIL
    latest_date   TEXT,                 -- 数据湖内容最新日（FAIL/缺失则 NULL）
    expected_date TEXT NOT NULL,        -- 期望交易日（比对基准）
    ready_at      TEXT NOT NULL,        -- 写入时间戳 ISO（调度诊断用）
    message       TEXT,                 -- 人类可读结论
    PRIMARY KEY (date, dataset)         -- 幂等 upsert：同日重采覆盖
)
```

CRUD（仿现有 `insert_trade_event`/`get_latest_action` 风格）：
- `upsert_data_ready(date, dataset, *, ok, melted, latest_date, expected_date, message, db_path=None)` —— 幂等 upsert（`INSERT OR REPLACE`，PK `(date, dataset)`）。
- `get_data_ready(date, dataset="daily", db_path=None) -> dict | None` —— 无记录返 None（未采集）。

**为什么独立表不复用 `trade_event`**：`trade_event` FK 绑 `account_id` + `symbol NOT NULL`，数据就绪事件无 account/symbol 语义，硬塞破坏引用完整性（`PRAGMA foreign_keys=ON`）。**为什么 upsert 不 append-only**：同日重采应反映最新结果而非堆叠历史；历史审计走钉钉/日志。

### 6.4 pre_open 三段式 gate（`engine.py`）

```python
async def _pre_open_gate(self, date: str, gw: Any) -> tuple[bool, str]:
    """三段 gate，全绿返 (True, "")；未绿返 (False, 原因)。
    顺序先便宜后贵：计划确认 → 网关健康 → 数据就绪。任一未绿即返，绝不触达网关写操作。
    """
    # ① 计划确认（读本地 JSON，最便宜）
    plan = load_plan(date)
    if not plan or not plan.get("confirmed"):
        return False, "无计划/未确认（人审闸）"
    # ② 网关健康（探测，无写副作用；复用韧性层 is_client_ready）
    if gw is None or not getattr(gw, "_connected", False):
        return False, "网关未连接"
    if not gw.is_client_ready():           # broker/qmt.py 文件 mtime 探测
        return False, "miniQMT 客户端未就绪"
    # ③ 数据就绪（DB 查询；防御性双检——§6.2 保证 plan 存在⇒数据已就绪）
    for k in self._plan_data_keys(plan):   # 从 plan 反推策略声明的 keys
        ready = get_data_ready(date, k)
        if ready is None or not ready["ok"]:
            return False, f"数据 {k} 未就绪（{ready['message'] if ready else '未采集'}）"
    return True, ""
```

`_plan_data_keys(plan)` 的实现（plan orders 携带 `experiment_id` 但不直接带 `strategy_name`，需经 resolver 反查）：
```python
def _plan_data_keys(self, plan: dict) -> set[str]:
    """从 plan 反推它所依赖的数据集 key 并集（防御性双检用）。

    plan orders 携带 experiment_id（engine.py:493），经 experiment resolver 反查
    strategy_name → build_strategy → required_data_keys。解析失败（reslover 异常/
    实验已 archive）→ 返 {"daily"}（保守默认，不阻断 gate 主流程——③ 本就是双检）。
    """
    keys: set[str] = set()
    try:
        from experiment.resolver import resolve_active
        exp_map = {e.id: e for e in resolve_active()}
        for o in plan.get("orders", []):
            exp = exp_map.get(o.get("experiment_id"))
            if exp is not None:
                strat = build_strategy(exp.strategy_name, exp.params)
                keys |= strat.required_data_keys
    except Exception:
        logger.exception("_plan_data_keys 解析失败，回退默认 {daily}")
    return keys or {"daily"}
```

接入 `pre_open` 入口（`engine.py:411`，`plan["confirmed"]` gate 之前）：
```python
gate_ok, gate_reason = await self._pre_open_gate(date, gw)
if not gate_ok:
    msg = f"pre_open gate 未通过：{gate_reason}，跳过挂单"
    logger.warning(msg)
    if self._mode == "live":
        fire_and_forget(notify_risk_event(msg, "CRITICAL"))
    return msg
```

> **注**：③ 数据就绪 gate 是防御性双检。因 §6.2 让 eod 在数据就绪后才产出 plan，理论上 plan 存在 ⇒ 数据已就绪。③ 防的是 plan 被人工手写/旧 plan 残留/跨日边界等异常。

---

## 7. lifespan 装配块（`presentation/server/main.py`）

仿现有 `replay_scheduler`/`training_orchestrator` 的 try/except 不阻断范式，在 lifespan 加 engine 装配块：

```python
# presentation/server/main.py lifespan（现有装配块之后追加）：
try:
    from trading.engine import TradingEngine, get_gateway
    from trading import dynamic_whitelist   # W1
    eng = TradingEngine()
    await eng.bootstrap()                   # W3: connect + DB init
    if check_shadow_gate():                 # W2: 影子期闸
        eng.start()
        app.state.trading_engine = eng
        logger.info("TradingEngine 已装配并启动")
    else:
        app.state.trading_engine = eng      # 装配但不起 scheduler（API 仍可用）
        logger.warning("TradingEngine 装配但 scheduler 未启动（影子期不足）")
except Exception:
    logging.getLogger(__name__).exception("TradingEngine 装配异常（已忽略）")

# ... shutdown 段（现有网关 disconnect 之后）：
_eng = getattr(app.state, "trading_engine", None)
if _eng is not None and _eng.sched.running:
    _eng.shutdown()
```

`trading/__main__.py` 保留为**开发/调试入口**（`python -m trading` 独立进程模式），但生产路径切到 uvicorn lifespan。`__main__` 改用 `check_shadow_gate()` + `eng.bootstrap()`（与 lifespan 同源，W2/W3 复用）。

---

## 8. 测试策略

### 8.1 单元测试
- `tests/trading/test_state_store_data_ready.py`（新）：`upsert_data_ready` 幂等（同日重采覆盖）；`get_data_ready` 命中/无记录返 None；多 dataset 独立行。
- `tests/trading/test_pipeline_then_eod.py`（新）：`pipeline_then_eod` 非交易日 no-op；采集失败（rc≠0）→ 未就绪 → 不调 `_eod`；就绪 → 调 `_eod`；多实验多策略 → keys 取并集；`required_data_keys` 默认 daily、覆盖后生效。
- `tests/trading/test_engine_pre_open_gate.py`（新）：无计划/未确认→跳过；网关未连→CRITICAL 跳过；客户端未就绪→CRITICAL 跳过；数据未就绪→CRITICAL 跳过；全绿→进挂单。
- `tests/trading/test_shadow_gate.py`（扩）：`check_shadow_gate` 返 bool（不再 sys.exit）；dry_run 放行；live 影子期不足→False。
- `tests/trading/test_dynamic_whitelist_w1.py`（新）：engine 实例属性注入/清空；`_submit` 传 whitelist 参数；server 路径不传 → 纯 env（隔离断言）。

### 8.2 e2e（扩 `tests/trading/test_e2e_trading_flow.py`）
`pipeline_then_eod` 跑采集（mock subprocess）→ 写 data_ready → eod 落 plan →（模拟次日）pre_open 三段 gate 全绿挂单。

### 8.3 集成验证（模拟盘可选，非 live 硬门）
- 用 `ops/data_pipeline.py` 实跑一次 → 验证 `data_ready` 表落了正确记录。
- 用 `trading/tools/trigger_eod_once.py` 验证 `_eod` 入口就绪校验：注入未就绪记录 → 跳过。

---

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 合并后 engine 与 server 同进程，engine 异常拖垮 API | lifespan 装配块 try/except 不阻断（与 replay_scheduler 同范式）；engine 逻辑各自带兜底（交易日/no-op 过滤）|
| W1 白名单参数透传遗漏某调用点 | grep 守护所有 `submit_order`/`_submit` 调用；单测断言 server 路径 whitelist=纯 env |
| Windows 子进程 PIPE 死锁（create_subprocess_exec） | 用 `stdout=DEVNULL`/重定向文件，不读 PIPE；采集日志已有文件落盘 |
| APScheduler misfire（uvicorn reload/restart 时 cron 错过）| `misfire_grace_time` 核查；盘后 job 错过靠 `pipeline_then_eod` 次触发窗口兜底 |
| 就绪事件写入失败（DB 锁/磁盘满）| supervisor 故障隔离不变；写失败记 ERROR+钉钉，eod 侧读 None → 不盲跑（比现状盲跑更安全）|
| `_pipeline_then_eod` 与残留 19:00 cron 双触发 | 合并后**删除独立 19:00 eod cron**，eod 只由 `pipeline_then_eod` 驱动（见 §10 阶段）|
| 引入 ops→trading 循环依赖 | `orchestrate/pipeline.py` import `data.freshness`（单向），不反向；ops 不改 |

---

## 10. 实施阶段（每阶段独立可测、可提交，给 writing-plans 当骨架）

1. **W3 bootstrap + W2 shadow_gate 返 bool**（无依赖，先清进程语义）— 封装 `bootstrap()`、`check_shadow_gate()`，`__main__` 复用。独立可测。
2. **W1 白名单实例属性 + 参数透传**（独立）— `_DYNAMIC` → `self._dynamic_whitelist`；`submit_order` 加 whitelist 参数；`_submit` 透传。
3. **S1 data_ready 表 + CRUD + T2 落库**（依赖 W1 之外的零）— state_store 第 7 张表；`run_data_check.py` T2 末尾 upsert。立即可独立验证。
4. **S2 `pipeline_then_eod` + `required_data_keys`**（依赖 W1/W3/S1）— `orchestrate/pipeline.py` 新文件；Strategy Protocol 加属性。
5. **S3 pre_open 三段 gate**（依赖 S1，与 S2 正交）— `_pre_open_gate` + 接入 `pre_open`。
6. **lifespan 装配块 + 删除独立 19:00 eod cron + schtasks 采集改子进程**（依赖 W1/W2/W3/S2）— 生产路径切 uvicorn。
7. **e2e + 钉钉实测收口**（依赖 S1-S3）。
8. （可选）模拟盘集成验证 → live（与韧性 spec §6 live gate 合并检查）。

---

## 11. 与 [[2026-07-29-trading-execution-resilience-design]] 的关系

- **正交**：韧性 spec 解决"网关自愈/撤单确认/口径漂移/静默告警"（执行层韧性）；本设计解决"数据→eod 信号链断裂 + pre_open 缺显式 gate + 调度器合并"（调度编排层）。两者可独立实施、独立测试。
- **协同点**：本设计 S3 pre_open 网关健康 gate 复用韧性 spec M1 的 `is_client_ready()`（`broker/qmt.py:311`）；S2/S3 的 CRITICAL 告警复用韧性 spec M4 的 `notify_risk_event` + `fire_and_forget`。
- **实施顺序建议**：韧性 spec（已部分落地，见 commit 0909b30d-cb34fb0a）优先；本设计（C-2 信号链 + 调度合并）次之。本设计 W1/W2/W3 与韧性 spec 无依赖冲突。
