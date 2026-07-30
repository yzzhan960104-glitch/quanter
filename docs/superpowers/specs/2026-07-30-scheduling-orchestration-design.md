# 调度编排层设计（Scheduling Orchestration，方向 B，解决 C-2）

> 日期：2026-07-30 ｜ 状态：待 review ｜ 作者：AI 研究员 + 用户
> 关联：[[2026-07-29-trading-execution-resilience-design]]（韧性层，与本设计正交）、[[plan-sqlite-deferred]]、[[eod-date-offbyone-fix]]
> 决策（已定，三项推荐项）：①就绪信号载体 = **DB 表**；②eod 触发 = **双保险**（保留 19:00 cron + 加 ready_watcher）；③**不合并**调度器，仅事件解耦。

## 1. 背景（事实，非推测）

### 1.1 三套调度器并行，无统一编排

| 轨 | 技术 | 位置 | 管 |
|---|---|---|---|
| 实时交易 | APScheduler `AsyncIOScheduler`（engine 进程内） | `trading/engine.py:1575-1655` | eod@19:00 / pre_open@09:22 / stoploss@30s / post_close@15:30 / health_guard@60s |
| 数据/观测 | Windows `schtasks`（系统任务计划，独立进程） | `ops/manage_ops_schtasks.py:28-30`、`discovery/schtasks.py:28-33` | data_pipeline@17:00 / brief@18:00 / discovery@02:00 |
| 开机自启 | `start_all.bat` → `ops/start_all.py` + Startup 快捷方式 | `ops/start_all.py:83-146` | 把上面两轨拉起并注册 |

### 1.2 数据采集 → eod 的信号链是断的（C-2 真正病灶）

采集进程（`ops/data_pipeline.py`，schtasks 触发）与 eod 进程（engine 内 APScheduler）是**两个独立进程，跨进程零 IPC，只共享文件系统**。现状靠**固定时间差赌博**：

- `data_pipeline.py` 串行跑 T1→采集→T2（`ops/data_pipeline.py:34-38`）；T2 检查有"熔断"语义，但结果**只变成进程退出码 2 + 钉钉，不落库**（`run_data_check`），eod 进程根本读不到。
- eod@19:00 直接 `pd.read_parquet(...)`（`engine.py:1849`），**不校验数据是否真采到**。注释（`engine.py:1593-1599`、`1805-1809`）自记：原本 15:35 触发会读到 T-1 旧数据算 T+1 计划（时序 bug），"修复"方式只是把 cron 挪到 19:00"等足"。采集超时/失败时 19:00 eod 照常跑、产废信号。
- 现成的 `check_freshness(key, expected_date)`（`data/freshness.py:39`）已能真正回答"T 日数据到没到"（比 parquet 内容最新日 vs 期望交易日），但**只在采集进程 T2 步骤跑，结果没落库、eod 订阅不到**。

### 1.3 pre_open 只有"计划确认"一道显式 gate

`pre_open`（`engine.py:550`）第一道硬 gate 是 `plan["confirmed"]`（`engine.py:580`），但**不显式检查网关健康、不显式检查数据就绪**——网关健康靠"进程启动 connect + 常驻 health_guard"（`engine.py:1657`）隐式保障，数据就绪靠"eod 排 19:00 等足"隐式保障。无任何"全绿才挂单"的显式前置 gate。

### 1.4 现成可复用件（避免重造轮子）

| 件 | 位置 | 复用价值 |
|---|---|---|
| `check_freshness(key, expected_date)` | `data/freshness.py:39` | 真正回答"T 日数据到没到"，直接作为就绪判据 |
| `is_client_ready()` 文件 mtime 探测范式 | `broker/qmt.py:311` | "纯探测无副作用"的范式，迁移成 data_ready 判活 |
| state_store `_connect` 上下文 + WAL + append-only 事件流 | `trading/state_store.py:46-67` | 新表建在此库，复用 `_connect`/迁移/幂等 UNIQUE 范式 |
| `trade_event` 表（append-only + action 枚举 + UNIQUE 幂等） | `trading/state_store.py:118-131` | 新表 schema 的设计模板（但**不复用该表**，语义不符：FK 绑 account/symbol） |
| `_health_guard` interval 自愈范式 | `trading/engine.py:1657` | ready_watcher 直接照此范式写（interval + no-op 前置过滤 + 退避） |

> **结论**：C-2 的最小必要修复 = 在采集侧把就绪信号结构化落库（DB 表），eod 从"19:00 时钟盲跑"改成"就绪事件到达才跑（19:00 cron + ready_watcher 双保险）"，pre_open 加显式三段式前置 gate。**不是**把三套调度器合并成 Airflow——那是 over-engineering，违背韧性 spec §2"不引入新依赖"。

## 2. 目标 / 非目标

**目标**
- **O1 数据就绪信号结构化**：采集完成（含 T2 检查）后在 state_store 写一条就绪事件，含 date/dataset/ok/melted/latest_date/expected_date/ready_at/message，跨进程可读、幂等可重跑。
- **O2 eod 事件驱动**：eod 从"19:00 时钟盲跑"改成"就绪事件到达才跑"；入口显式校验就绪，未就绪则跳过（不读旧 parquet 产废信号）；超时告警。19:00 cron 保留作确定性兜底。
- **O3 pre_open 三段式显式 gate**：计划确认 + 网关健康 + 数据就绪，全绿才挂单；任一未绿记 WARNING/CRITICAL 并跳过（live 模式）。
- **O4 可观测**：就绪/等待/超时/熔断事件落库 + 钉钉（CRITICAL），取代当前"进程退出码 + 事后发现"。

**非目标（YAGNI，本次不做）**
- 不引入 Airflow / Prefect / Celery / Redis 等重型框架/中间件（违背韧性 spec §2）。
- 不把 schtasks 数据轨合并进 engine 进程（爆炸半径：数据/discovery 崩了拖垮交易核心；关注点分离保留，决策点 ③）。
- 不改交易日内四阶段的业务算法（eod/pre_open/post_close 算法不动，只改触发与 gate 机制）。
- 不改数据采集算法（`sync_daily_incremental` 不动，只在 supervisor 末尾"接线"写就绪信号）。
- 不改 plan 存储（仍 JSON 文件 `logs/trading_plans/plan_<date>.json`，[[plan-sqlite-deferred]] 独立任务）。

## 3. 总体架构：事件通道 + 订阅者（保留双轨）

保留双轨调度器（交易轨 APScheduler + 数据轨 schtasks），在二者间建立一条**轻量跨进程就绪事件通道（DB 表 + engine 侧轮询）**，把"时间差赌博"换成"显式事件依赖"：

```
┌──────────── 数据轨（schtasks，独立进程）────────────┐
│ ops/data_pipeline.py supervisor（已存在，串行）       │
│   ① T1 检查 → ② 采集 → ③ T2 检查                      │
│                          │                            │
│   【S1 新增】T2 末尾写就绪事件到 state_store            │
│   复用 check_freshness 作判据 + 采 T2 熔断状态          │
│                          ▼                            │
│          ┌──────────────────────────────┐            │
│          │ state_store.data_ready 表     │  ← 唯一    │
│          │ (date, dataset) PK 幂等 upsert │   显式耦合 │
│          └──────────────────────────────┘            │
└──────────────────────────┬───────────────────────────┘
                           │ 跨进程读取（轮询 60s）
┌──────────────────────────▼───────────────────────────┐
│ 交易轨（engine 进程内 APScheduler）                    │
│                                                       │
│  【S2 新增】_ready_watcher job（IntervalTrigger 60s）   │
│     交易日盘后窗口轮询 → ok 且未 consumed → 触发 _eod    │
│                                                       │
│  eod（双保险：19:00 cron 保留 + ready_watcher 驱动）    │
│     入口 assert_data_ready(T) → 扫信号 → 落 plan        │
│                                                       │
│  pre_open@09:22（时钟不变）                            │
│     【S3 新增】_pre_open_gate：计划确认+网关+数据 三段   │
└───────────────────────────────────────────────────────┘
```

**为什么"轮询订阅"而非"实时推送"**：APScheduler 无 DB LISTEN/跨进程信号能力，engine 是常驻异步进程，用一个轻量 interval job 轮询就绪状态是最小代价实现，天然契合现有 `_health_guard`（`engine.py:1657`，同样 60s interval 自愈）的范式——可复用同一套模式（前置 no-op 过滤 + 退避），不引入新依赖。

## 4. 模块设计

### S1 · 数据就绪信号（采集侧落库）— 事件源

#### 4.1.1 新增表 `data_ready`（state_store 第 7 张表）

DDL（仿 `trade_event` 的 append-only + UNIQUE 幂等范式，但 **upsert** 语义而非 append-only，因同日重采应覆盖；放 `trading/state_store.py` `init_store` 内）：

```sql
CREATE TABLE IF NOT EXISTS data_ready (
    date          TEXT NOT NULL,        -- T（YYYY-MM-DD，交易日）
    dataset       TEXT NOT NULL,        -- registry 语义 key（如 "daily"）
    ok            INTEGER NOT NULL,     -- 1=就绪（latest_date>=expected）；0=缺失/陈旧
    melted        INTEGER NOT NULL DEFAULT 0,  -- 1=T2 熔断（超 deadline 仍 FAIL）
    latest_date   TEXT,                 -- 数据湖内容最新日（FAIL/缺失则 NULL）
    expected_date TEXT NOT NULL,        -- 期望交易日（比对基准）
    ready_at      TEXT NOT NULL,        -- 写入时间戳 ISO（调度诊断用）
    message       TEXT,                 -- 人类可读结论（含告警/排查信息）
    PRIMARY KEY (date, dataset)         -- 幂等 upsert：同日重采覆盖
)
```

**为什么独立表不复用 `trade_event`**：`trade_event` FK 绑 `account_id`（`state_store.py:120`）+ `symbol NOT NULL`（`:122`），数据就绪事件没有 account/symbol 语义，硬塞破坏引用完整性（`PRAGMA foreign_keys=ON`，`:60`）。

**为什么 upsert 不 append-only**：数据同日可能重采多次（T2 FAIL → 重采 → PASS），就绪状态应反映**最新结果**而非堆叠历史（`data_ready` 的语义是"当前快照"）。历史审计走钉钉/日志，不靠此表。

#### 4.1.2 新增 CRUD（state_store.py，仿现有 `insert_trade_event`/`get_latest_action` 风格）

```python
def upsert_data_ready(date: str, dataset: str, *, ok: bool, melted: bool,
                      latest_date: str | None, expected_date: str,
                      message: str, db_path: str | None = None) -> None:
    """幂等写数据就绪事件。同日重采覆盖（PRIMARY KEY (date, dataset) ON CONFLICT REPLACE）。"""
    ready_at = datetime.now().isoformat(timespec="seconds")
    with _connect(db_path or _DEFAULT_DB) as con:
        con.execute(
            "INSERT OR REPLACE INTO data_ready "
            "(date, dataset, ok, melted, latest_date, expected_date, ready_at, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (date, dataset, int(ok), int(melted), latest_date, expected_date,
             ready_at, message),
        )

def get_data_ready(date: str, dataset: str = "daily",
                   db_path: str | None = None) -> dict | None:
    """读某日某数据集就绪事件。无记录返 None（未采集）。返回 dict 含 ok/melted/message 等。"""
    with _connect(db_path or _DEFAULT_DB) as con:
        row = con.execute(
            "SELECT * FROM data_ready WHERE date=? AND dataset=?",
            (date, dataset),
        ).fetchone()
    return dict(row) if row else None
```

#### 4.1.3 接入采集 supervisor（`ops/data_pipeline.py` 末尾）

在 `main()` 汇总段（`ops/data_pipeline.py:51-55`）后追加写就绪事件。复用 `check_freshness`（不重造轮子）+ 采 T2 熔断状态（从 T2 步骤 rc 推断：rc==2 → melted）：

```python
def _write_data_ready(rcs: list[tuple[str, int]]) -> None:
    """S1：supervisor 末尾写数据就绪事件（复用 check_freshness + 采 T2 熔断状态）。"""
    from trading.state_store import upsert_data_ready
    from data.freshness import check_freshness
    from calendar_utils import expected_latest_trade_day  # T 日期望交易日
    from datetime import date as _date
    today = str(_date.today())
    expected = expected_latest_trade_day(today)        # 复用现有交易日历口径
    fr = check_freshness("daily", expected)            # 真正判据（不靠 mtime）
    t2_melted = any(name.startswith("③") and rc == 2 for name, rc in rcs)  # T2 rc==2
    upsert_data_ready(
        today, "daily", ok=fr.ok, melted=t2_melted,
        latest_date=fr.latest_date, expected_date=expected,
        message=fr.message,
    )
```

> **边界**：supervisor 是同步进程，直接 import + 调用，无需异步。`check_freshness` 内部 `read_parquet` ~1.75s（455MB），低频可接受（`data/freshness.py:9-11` 已注明）。

### S2 · eod 事件订阅（engine 侧）— 双保险取代盲跑

#### 4.2.1 新增 `_ready_watcher` job（注册在 `__init__`，仿 `_health_guard`）

```python
# engine.py __init__ 内，_health_guard add_job 之后追加：
self.sched.add_job(
    self._ready_watcher,
    IntervalTrigger(
        seconds=int(os.getenv("ENGINE_READY_WATCHER_INTERVAL_SECONDS", "60"))),
    id="ready_watcher",
)
# 进程内去重（防 ready_watcher 与 19:00 cron 双触发重复跑 eod）
self._eod_done_dates: set[str] = set()
```

`_ready_watcher` 逻辑（前置 no-op 过滤 + 触发，完全照 `_health_guard` 范式）：

```python
async def _ready_watcher(self) -> None:
    """S2：数据就绪订阅——交易日盘后窗口轮询 data_ready，就绪且当日 eod 未跑→触发 _eod。
    
    范式对齐 _health_guard：interval job + 前置 no-op 过滤 + 进程内去重。
    窗口约束（18:00-21:00）下放此处判据，非窗口期直接 no-op（零副作用）。
    """
    today = str(date.today())
    # no-op 1：非交易日（_eod 内已守，此处早退省一次 DB 查询）
    if not calendar.is_trading_day(today):
        return
    # no-op 2：窗口外（盘前/盘中/深夜不轮询，省 DB 查询）
    now = datetime.now()
    if not (18 <= now.hour < 21):          # 盘后窗口 18:00-21:00（env 可调）
        return
    # no-op 3：当日 eod 已跑过（双保险防 ready_watcher 与 19:00 cron 双触发重复跑）
    if today in self._eod_done_dates:
        return
    # 超时告警：21:00 仍未就绪 → CRITICAL（数据采集异常，eod 无法产出）
    ready = get_data_ready(today, "daily")
    if ready is None:
        return                             # 采集还没跑，等下轮
    if not ready["ok"] or ready["melted"]:
        if now.hour >= 20:                 # 20:00 后仍未就绪，告警（21:00 前留缓冲）
            fire_and_forget(notify_risk_event(
                f"数据未就绪/熔断：{ready['message']}，eod 无法产出", "CRITICAL"))
        return
    # 就绪且未跑 → 触发 eod
    await self._eod()
    self._eod_done_dates.add(today)
```

#### 4.2.2 `_eod` 入口加显式就绪校验（双保险：19:00 cron 触发时也校验）

在 `_eod`（`engine.py:1792`）入口（交易日守卫之后、`read_parquet` 之前）加：

```python
# S2：显式校验数据就绪（消除"盲跑旧 parquet"——C-2 核心痛点）
ready = get_data_ready(today, "daily")
if ready is None or not ready["ok"]:
    msg = f"eod 触发但数据未就绪（{ready['message'] if ready else '采集未跑'}），跳过防读旧数据"
    logger.warning(msg)
    fire_and_forget(notify_risk_event(msg, "WARNING"))
    return                                # 不 read_parquet，不产废信号
```

**双保险语义**：
- 19:00 cron 触发：若数据已就绪 → 正常跑（路径不变，仅多一道校验）；若未就绪 → 跳过，交给 ready_watcher 等待。
- ready_watcher 触发：数据就绪后跑。`_eod_done_dates` 去重防与 19:00 cron 重复。

### S3 · pre_open 三段式前置 gate — 全绿才挂单

在 `pre_open`（`engine.py:550`）入口，现有 `plan["confirmed"]` gate（`engine.py:577-582`）**之前**加 `_pre_open_gate`（计划确认仍是第一道，但网关/数据检查显式化并前置到任何网关写操作之前）：

```python
async def _pre_open_gate(self, date: str, gw: Any) -> tuple[bool, str]:
    """S3：pre_open 三段式前置 gate。全绿返 (True, "")；未绿返 (False, 原因)。
    
    严格顺序（先便宜后贵）：计划确认（读 JSON）→ 网关健康（探测）→ 数据就绪（DB 查询）。
    任一未绿即返，绝不触达网关写操作（撤单/挂单）。
    """
    # ① 计划确认（第一道，最便宜——读本地 JSON）
    plan = load_plan(date)
    if not plan:
        return False, "无计划"
    if not plan.get("confirmed"):
        return False, "计划未确认（人审闸）"
    # ② 网关健康（探测，无写副作用）
    if gw is None or not getattr(gw, "_connected", False):
        return False, "网关未连接"
    if not gw.is_client_ready():           # 复用 broker/qmt.py:311 文件探测
        return False, "miniQMT 客户端未就绪"
    # ③ 数据就绪（DB 查询，防御性双检——S2 已保证 plan 存在⇒数据就绪）
    ready = get_data_ready(date, "daily")
    if ready is None or not ready["ok"]:
        return False, f"数据未就绪（{ready['message'] if ready else '未采集'}）"
    return True, ""
```

**接入 `pre_open`**（`engine.py:577` 前）：

```python
gate_ok, gate_reason = await self._pre_open_gate(date, gw)
if not gate_ok:
    msg = f"pre_open gate 未通过：{gate_reason}，跳过挂单"
    logger.warning(msg)
    if self._mode == "live":               # live 模式致命事件告警
        fire_and_forget(notify_risk_event(msg, "CRITICAL"))
    return msg
```

> **注**：S3 数据就绪 gate 是防御性双检。因 S2 让 eod 在数据就绪后才产出 plan，理论上 plan 存在 ⇒ 数据已就绪。S3 防的是 plan 被人工手写/旧 plan 残留/跨日边界等异常。

### S4 · 编排收口（保守，不强合并）

按决策点 ③ **不合并** schtasks 到 engine。仅做两件低成本收口：
1. `start_all.bat` / `ops/start_all.py` 注释明确化：数据轨 schtasks 与 engine 的依赖关系 = 就绪事件通道（取代 parquet 文件隐式耦合）。
2. `ops/data_pipeline.py` 顶部注释更新：supervisor 末尾写就绪事件是"两轨唯一显式耦合点"。

## 5. 测试策略（对齐韧性 spec §5 风格）

### 5.1 单元测试

- `tests/trading/test_state_store_data_ready.py`（新）：`upsert_data_ready` 幂等（同日重采覆盖）；`get_data_ready` 命中/无记录返 None；`check_freshness` 注入临时 parquet → ok/陈旧/缺失三态；T2 rc==2 → melted=1。
- `tests/trading/test_engine_ready_watcher.py`（新）：非交易日 no-op；窗口外 no-op；当日 eod 已跑（`_eod_done_dates`）no-op；未就绪 no-op + 20:00 后告警；就绪且未跑 → 触发 `_eod` + 入 `_eod_done_dates`；双触发去重（ready_watcher 与 19:00 cron 同日不重复跑）。
- `tests/trading/test_engine_pre_open_gate.py`（新）：无计划→跳过；计划未确认→跳过；网关未连→CRITICAL 跳过；客户端未就绪→CRITICAL 跳过；数据未就绪→CRITICAL 跳过；全绿→进挂单。

### 5.2 e2e（扩 `tests/trading/test_e2e_trading_flow.py`）

采集写就绪事件 → ready_watcher 触发 eod → 落 plan →（模拟次日）pre_open 三段 gate 全绿挂单。

### 5.3 集成验证（模拟盘可选，非 live 硬门）

- 用 `ops/data_pipeline.py` 实跑一次 → 验证 `data_ready` 表落了正确记录（`check_freshness` 判据 + T2 熔断状态）。
- 用 `trading/tools/trigger_eod_once.py`（不连网关不下单，`trigger_eod_once.py:49-79`）验证 `_eod` 入口就绪校验：注入未就绪记录 → 跳过 + WARNING。

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| ready_watcher 与 19:00 cron 双触发重复跑 eod | `_eod_done_dates` 进程内去重（同日只跑一次）|
| ready_watcher 60s 轮询空跑刷 DB | 前置 no-op（非交易日/窗口外/已跑 三道过滤），窗口内最多约 180 次查询/天（18:00-21:00），可忽略 |
| 就绪事件写入失败（DB 锁/磁盘满） | supervisor 已有故障隔离（`data_pipeline.py:48-50` 不阻断后续）；写失败记 ERROR+钉钉，eod 侧读 None → 等待不盲跑（比现状盲跑更安全）|
| T2 熔断状态从 rc 推断不准（rc 语义漂移） | 直接复用 `check_freshness` 判据作主真相，melted 仅作附加标记；单测覆盖 rc==2 → melted=1 |
| 数据轨与交易轨时间漂移（采集越来越晚） | ready_watcher 窗口到 21:00 + 超时 CRITICAL 告警，人工介入（不自动降级读旧数据）|
| 引入对 state_store 的循环依赖（ops→trading） | `ops/data_pipeline.py` import `trading.state_store` 单向，无循环（state_store 不反向依赖 ops）|

## 7. 实施阶段（每阶段独立可测、可提交，给 writing-plans 当骨架）

按依赖顺序，每阶段独立可测、可提交：

1. **S1 数据就绪信号**（无依赖，先建事件源）— 新增 `data_ready` 表 + CRUD + 接入 `data_pipeline.py` 末尾。立即可独立验证（跑 supervisor 看表）。
2. **S2 ready_watcher + eod 就绪校验**（依赖 S1）— 新增 `_ready_watcher` job + `_eod` 入口校验 + `_eod_done_dates` 去重。
3. **S3 pre_open 三段 gate**（独立，与 S2 正交）— 新增 `_pre_open_gate` + 接入 `pre_open`。
4. **S4 编排收口注释**（无依赖，可并入 S1 一起提）。
5. **e2e + 钉钉实测收口**（依赖 S1-S3）。
6. （可选）模拟盘集成验证 → live（与本设计正交的韧性 spec §6 live gate 合并检查）。

## 8. 与 [[2026-07-29-trading-execution-resilience-design]] 的关系

- **正交**：韧性 spec 解决"网关自愈/撤单确认/口径漂移/静默告警"（执行层韧性）；本设计解决"数据→eod 信号链断裂 + pre_open 缺显式 gate"（调度编排层）。两者可独立实施、独立测试。
- **协同点**：本设计 S3 pre_open 网关健康 gate 复用韧性 spec M1 的 `is_client_ready()`（`broker/qmt.py:311`）；S2/S3 的 CRITICAL 告警复用韧性 spec M4 的 `notify_risk_event` + `fire_and_forget`。
- **实施顺序建议**：韧性 spec（已部分落地，见 commit 0909b30d-cb34fb0a）优先（P0 网关死锁是更直接的故障源）；本设计（C-2 数据信号链）次之。
