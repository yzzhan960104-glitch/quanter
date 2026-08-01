# C-8 全 job 启动补跑（采集/eod/pre_open/brief 最终一致性）

- **日期**：2026-08-02
- **分支**：feat/c8-startup-catchup（spec 阶段）
- **状态**：待审（spec review gate）
- **关联**：
  - C-7 spec/plan（start_all 收编 + discovery 启动补跑——`_discovery_missed_last_run` 先例）
  - C-4 spec（L1 停调度 / R2「盘后 job 错过=机器故障，补跑危险」——C-8 仅对 18:00 pipeline 链的**启动补跑**场景定向覆盖，post_close 立场不变）
  - C-2 spec（`pipeline_then_eod` 事件链：采集→校验→data_ready→eod→brief）
  - C-6 spec（clock 单一时间源 + 触发点入口缓存）
- **范围**：lifespan 启动补跑 = pipeline 链（采集→校验→data_ready→eod→brief）+ pre_open；新增 job 运行台账（running/done/skipped/failed）；事件链日期参数化最小改造。

---

## 1. 背景与现状

### 1.1 痛点
1. **生产机不 7x24**（会关机/断电）：跨 18:00 pipeline、09:22 pre_open、15:30 post_close、02:00 discovery 任一触发点时，当日 job 漏跑。
2. **C-7 只补 discovery**：采集/eod/pre_open/brief 漏跑无补偿——eod 漏 → 次日无计划 → pre_open gate「无计划」整天不挂单；采集漏 → data_ready 缺 → 数据链断；brief 漏 → 研究员无播报。
3. **无 job 运行台账**：漏跑判定只能靠产物反推（plan 文件 / data_ready / `.last_*_brief`），语义模糊——「无 plan」可能是无信号而非没跑，多源拼接脆弱。
4. **事件链日期口径固定**：`pipeline_then_eod` / `engine._eod` 内部硬用 `clock.today()` / `clock.trading_day()`——T+1 早上补跑 T 日链会产出 T+2 废计划 + data_ready 错位（C-6 同源 key 错位风险）。

### 1.2 现状（master HEAD 714c95dc，C-7 merged）
| 项 | 现状 | 证据 |
|---|---|---|
| 触发点 | pipeline_then_eod 18:00 / pre_open 09:22 / stop_loss 30s / post_close 15:30 / _health_guard 60s | `trading/engine.py` TradingEngine.__init__ |
| pipeline 链 | 采集子进程 `ops/data_pipeline.py` → check_freshness → upsert_data_ready(today) → engine._eod() → run_brief_all() | `trading/orchestrate/pipeline.py` |
| 日期口径 | clock.today（读）/ clock.trading_day（eod 落盘） | C-6 |
| discovery 补跑 | `_discovery_missed_last_run` + 启动异步补跑（DETACHED subprocess） | `presentation/server/main.py` C-7 V3 |
| 幂等 | order/trade_event/fill UNIQUE + has_order + `.last_<bot>_brief` 文件 | C-1 / broadcast |
| 采集日期语义 | `expected_latest_trade_day(now)` 自动对准最近已收盘交易日（T+1 早上跑也查 T 日） | `trading/calendar.py` / `data/tools/run_data_check.py` |

**核心病灶**：除 discovery 外无启动补跑；补跑所需的日期参数化（for_date / data_day / plan_date）不存在；无跨重启的 job 运行台账。

---

## 2. 目标与非目标

### 目标
1. **job 运行台账**（`trading/job_ledger.py`）：pipeline / pre_open 两档运行状态，跨重启持久，cron 与补跑共用。
2. **日期参数化**（最小改造）：`pipeline_then_eod(for_date)` + `TradingEngine._eod(data_day, plan_date)`，默认路径（None）行为零变化。
3. **lifespan 启动后台补跑**（`trading/catchup.py`）：
   - pipeline：`D = expected_latest_trade_day(now)`，D 未 done 且其 18:00 已过 → 补 采集→校验→data_ready→eod→brief；
   - eod 裁剪（政策 A）：`plan_date = next_trading_day(D)`；plan_date ≤ 今天 且 now > 窗口截止 → 跳过 eod（只补数据 + brief）；窗口内 → 全链 + pre_open；
   - pre_open：今天是交易日 且 now ∈ [09:22, 窗口截止) 且未 done → 补跑；
   - brief：pipeline 链尾自带 + 独立兜底（pipeline done 但 `.last_<bot>_brief` < D → 补播一次）。
4. **幂等**：cron 与补跑共享台账守卫（running/done 跳过）；启动重置遗留 running。
5. **失败语义**：补跑失败 → 台账 failed + CRITICAL 告警，不停调度、不阻断 uvicorn（cron 路径 L1 语义不变）。

### 非目标（显式 out of scope）
- **不做 post_close / stop_loss / discovery 补跑**（post_close 沿用 C-4 R2；discovery 已有 C-7）。
- **不逐日补历史**（政策 A：只补最近一致态；采集增量天然回填全部数据缺口）。
- **不改 ops/data_pipeline.py / run_data_check**（`expected_latest_trade_day` 已天然对准最近已收盘交易日，无需日期参数）。
- **不改 _critical_guard / gate / clock**（C-4/C-5/C-6 决议不变）。
- **不引入 APScheduler jobstore**（方案 3 否决：无法实现「早上开机补 09:22」，且侵入调度器）。

---

## 3. 架构

### 3.1 job 运行台账（trading/job_ledger.py）
- **DB 路径**：`logs/trading_job_run.db`，env `TRADING_JOB_LEDGER_DB` 覆盖；模块级 `_DEFAULT_DB_PATH` + None-fallback 范式（同 `backtest/tasks_db.py`，便于测试 monkeypatch 隔离）。
- **表结构**：
  ```sql
  CREATE TABLE IF NOT EXISTS job_run (
    job_name      TEXT NOT NULL,   -- "pipeline" | "pre_open"
    business_date TEXT NOT NULL,   -- pipeline=数据日 T；pre_open=业务日
    status        TEXT NOT NULL,   -- running | done | skipped | failed
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    message       TEXT,
    PRIMARY KEY (job_name, business_date)
  );
  ```
- **API**：`init_db()` / `begin_run(job, date, started_at)`（INSERT OR REPLACE status=running）/ `finish_run(job, date, status, message="")` / `latest_status(job, date) -> str | None` / `reset_stale_running()`（启动时把遗留 running → failed("interrupted")，防崩溃残留永久阻塞）。
- **状态语义**：
  - pipeline：running → done（全链完成，含 run_eod=False 的裁剪态）｜failed（采集 rc≠0 / data 未就绪 / 异常）；
  - pre_open：running → done（流程正常完成，含无单可挂）｜skipped（gate 未过：无计划/未确认/网关未连/数据未就绪）｜failed（异常）。

### 3.2 日期参数化（最小改造）

**`pipeline_then_eod(engine, *, for_date=None, run_eod=True)`**（`trading/orchestrate/pipeline.py`）：
- `today = for_date or clock.today()`（延续 C-6 入口缓存语义）；
- gate `is_trading_day(today)`；
- `upsert_data_ready(today, ...)`（原 `clock.today()` → 参数化日期）；
- `for_date` 提供时：`await engine._eod(data_day=today, plan_date=calendar.next_trading_day(today))`；否则保持 `engine._eod()`（行为零变化）；
- `run_eod=False`（窗口已过只补数据时）：跳过 eod 段，仍走 采集→校验→data_ready→brief。

**`TradingEngine._eod(*, data_day=None, plan_date=None)`**（`trading/engine.py`）：
- `_today = data_day or clock.today()`：交易日 gate + df_upto 截止 + integrity ctx + cooldown + scan_live 截止全部沿用 `_today`；
- `_td = plan_date or clock.trading_day()`：eod_plan 落盘 key；
- 默认 None → 现行为逐字节不变；周六补周五链时 `data_day=周五`（gate 通过）、`plan_date=周一`。

### 3.3 补跑判定与编排（trading/catchup.py）

`async def run_startup_catchup(engine) -> dict`：
1. `reset_stale_running()`（进程崩溃遗留 running 先清场）；
2. `D = expected_latest_trade_day(clock.now())`（最近已收盘交易日 = pipeline 数据日）；
3. **pipeline 补跑**：`latest_status("pipeline", D) not in ("running", "done")` 且（`D < today` 或 `now >= 18:00`）→
   `await pipeline_then_eod(engine, for_date=D, run_eod=(plan_date 未过窗口))`；
   - `plan_date = calendar.next_trading_day(D)`；
   - plan_date ≤ 今天 且 now > 窗口截止 → `run_eod=False`（不产过期计划）；否则 `run_eod=True`；
   - `D == today 且 now < 18:00` → 不补跑（今晚 cron 正常处理，避免 16:00 提前拉未清算数据）；
4. **brief 独立兜底**：`latest_status("pipeline", D) == "done"` 且任一 `.last_<bot>_brief` 文件缺失或内容 < D → `await run_brief_all()`（幂等文件去重）；
5. **pre_open 补跑**：今天是交易日 且 09:22 ≤ now < 窗口截止 且 `latest_status("pre_open", today) not in ("running", "done")` → `await pre_open(today)`（模块级函数）；
   - 窗口已过且未 done → CRITICAL 告警「今日 pre_open 窗口已过，不补挂单」（政策 A 显式知会，不静默）；
6. **失败语义**：全任务 try/except → 台账 failed + CRITICAL（`infra.notifier` fire_and_forget），不 raise、不 halt、不阻断 uvicorn。

**为什么补跑调模块级 `pre_open(today)` / `pipeline_then_eod` 而非 engine 的 `@_critical_guard` 包装**：
engine 包装的 L1 异常会停调度（C-4）；补跑失败语义定为「留今晚 18:00 cron 自然收敛」——停调度会连今晚的收敛机会一起杀掉（review 点 2）。

### 3.4 台账写入点（cron 与补跑共用）
- `pipeline_then_eod` 入口 `begin_run("pipeline", today)`；完成（eod 段结束后）`finish_run(..., "done")`；采集 rc≠0 抛 `_CriticalHalt` 前 / `not all_ok` / 其它异常 → `finish_run(..., "failed", message)` 后再上抛。
- 模块级 `pre_open(date)` 入口 `begin_run("pre_open", date)`；gate 未过 → `finish_run(..., "skipped", reason)`；主流程完成 → `"done"`；异常 → `"failed"` 后向上抛（cron 路径由 `_critical_guard` 按 C-4 L1 停调度；catchup 路径捕获转 failed）。

### 3.5 幂等与并发
- **cron 路径入口守卫**：`pipeline_then_eod` / `pre_open` 入口查 `latest_status in ("running", "done")` → 跳过（防御性；按时间线分析 cron 与补跑实际不会同日期并发，守卫兜底双跑）。
- **启动重置**：`reset_stale_running()` 防崩溃残留 running 永久阻塞（同 training_loops_db.reset_interrupted 范式）。
- **补跑单例**：lifespan 仅 `asyncio.create_task` 一次；shutdown 段 `cancel()`。

### 3.6 lifespan 接线（presentation/server/main.py）
engine 装配 + start 后、discovery 补跑块附近：
```python
try:
    from trading.catchup import run_startup_catchup
    _eng_cu = getattr(app.state, "trading_engine", None)
    if _eng_cu is not None and getattr(_eng_cu.sched, "running", False):
        app.state.catchup_task = asyncio.create_task(run_startup_catchup(_eng_cu))
        logging.getLogger(__name__).info("C-8 启动补跑任务已创建")
except Exception:
    logging.getLogger(__name__).exception("C-8 启动补跑创建异常（已忽略）")
```
shutdown 段：`app.state.catchup_task.cancel()`（try/except，软降级）。

### 3.7 不变量
- 默认路径（for_date / data_day / plan_date 均为 None）行为零变化。
- cron 路径 L1 停调度语义不变（C-4）。
- gate / clock / 幂等键（C-4/C-5/C-6）不变。
- 补跑只在 lifespan startup 触发一次（不引入新常驻循环）。

---

## 4. 测试策略

- **test_job_ledger.py**（新）：CRUD + UNIQUE 覆盖 + reset_stale_running + tmp_path 隔离。
- **test_catchup.py**（新）：
  - pipeline 漏/未漏两态；`D == today 且 now < 18:00` 不补跑；
  - plan 裁剪：now > 窗口截止 → run_eod=False；窗口内 → 全链；
  - 周末补跑：D=周五 → eod plan_date=周一；
  - pre_open 窗口两态（内/外）+ skipped 可重试（09:22 cron gate 跳过 → 补跑重试）；
  - 编排顺序：pipeline 先于 pre_open（同日窗口场景）；
  - 失败语义：pipeline 异常 → failed + CRITICAL + 不 halt + 不阻断；
  - brief 独立兜底：`.last` 文件 < D → run_brief_all 恰一次。
- **test_pipeline_date_param.py**（新）：for_date → upsert_data_ready(D) + engine._eod(data_day=D, plan_date=next(D))；None 默认路径不变。
- **engine._eod 参数化**：data_day / plan_date 显式用例（gate 用 data_day；落盘 key 用 plan_date）。
- **test_lifespan_consolidation.py 增补**：create_task 接线 + shutdown cancel。
- **全量回归**：C-7 后 1180 passed 基线零退化。

---

## 5. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 09:21 boot：09:22 cron 无计划 gate 跳过 → 补跑链完成后须再补 pre_open | skipped 不算 done；补跑在窗口内重试（判定在任务执行时点求值） |
| R2 | 补跑采集失败 / data 未就绪 | failed + CRITICAL；不停调度留今晚 18:00 cron 收敛；未产计划天然 fail-closed（pre_open gate 拦） |
| R3 | 进程崩溃遗留 running 阻塞后续 | reset_stale_running 启动重置 |
| R4 | 周末/长假补跑产计划日期错位 | next_trading_day 口径 + 周末场景测试覆盖 |
| R5 | 补跑与 18:00 cron 同日期并发双跑 | 台账 running/done 守卫 + 时间线分析（cron 触发时补跑目标日期必为昨天或更早，实际错开） |
| R6 | 窗口内晚写熔断基线口径失真 | review 已接受：不写则 post_close 熔断整体失效更糟（见 review 点 4） |
| R7 | 日期参数化误改默认路径 | 默认 None 分支测试锁定 + 全量回归 1180/0 |

---

## 6. 验收标准

1. `job_run` 台账落库（pipeline/pre_open 两档），状态机 running/done/skipped/failed + 启动重置。
2. `pipeline_then_eod(for_date, run_eod)` / `engine._eod(data_day, plan_date)` 参数化，默认路径零变化（既有测试全绿）。
3. lifespan 启动后台补跑：pipeline(D) 未 done → 采集→data_ready→eod→brief；窗口已过 → run_eod=False 只补数据+brief。
4. pre_open 补跑窗口 [09:22, 窗口截止)（`ENGINE_PRE_OPEN_CATCHUP_UNTIL` 默认 10:00，env 可调），gate/幂等沿用；skipped 可重试。
5. brief 独立兜底（`.last_<bot>_brief` < D → 补播一次，幂等文件去重）。
6. cron vs 补跑无双跑（台账守卫）。
7. 补跑失败 → failed + CRITICAL，不 halt、不阻断 uvicorn；cron 路径 L1 不变。
8. 全量回归 1180 passed / 0 failed。

---

## 7. 实现步骤（高层 · 详细 diff 见 plan）

| 阶段 | 内容 | gate |
|---|---|---|
| **V1 job_ledger** | `trading/job_ledger.py`（表/API/reset）+ 单测 | ledger 单测 |
| **V2 日期参数化 + pipeline 台账** | `pipeline_then_eod(for_date, run_eod)` + `engine._eod(data_day, plan_date)` + pipeline 台账写入 + 测试 | 参数化单测 + 相关子集 |
| **V3 pre_open 台账** | 模块级 `pre_open(date)` 写入 running/done/skipped/failed + 测试 | pre_open 单测 |
| **V4 catchup 编排** | `trading/catchup.py`（判定/裁剪/顺序/brief 兜底/失败语义）+ 单测 | catchup 单测 |
| **V5 lifespan + 全量回归** | main.py create_task/cancel + 全量 1180/0 + spec §6 验收 1-8 | smoke + 1180/0 |

---

## 8. spec review 要点

1. **补跑失败不停调度**（failed + CRITICAL + 留今晚 18:00 cron 自然收敛；cron 路径 L1 不变）—— 接受？
2. **补跑调模块级 pre_open / pipeline_then_eod**（不经 `_critical_guard`，与 cron 路径 L1 停调度隔离）—— 接受？
3. **台账状态机 running/done/skipped/failed + 启动重置 stale running** —— 接受？
4. **pre_open 窗口内补跑仍写熔断基线**（晚于开盘但仍是当日最早可用锚点；不写则 post_close 熔断整体失效，更糟）—— 确认（brainstorm 已接受，固化）？
5. **周末/盘前补跑产「下一交易日」计划**（周六补周五链 → 产周一计划）—— 确认（brainstorm 已接受，固化）？

spec 通过后落 plan（`docs/superpowers/plans/2026-08-02-c8-startup-catchup.md`）。
