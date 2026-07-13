# Spec 1 · 蔡森回测异步化设计（参数训练平台 · Spec 1）

> 2026-07-13 · brainstorm 阶段产出 · 用户已认可，转入实现计划

## 1. 背景与定位

蔡森参数训练平台终态愿景：跑全市场回测 → AI 分析 → 调参 → 再跑，用几天闭环训练出最优参数组合。分解为 4 个独立 spec（详见项目记忆 `quanter-param-training-platform`）：

- **Spec 1 回测异步化**（本文档 · 闭环地基）
- Spec 2 Parameter Lab 前端工作台
- Spec 3 AI 分析模块
- Spec 4 闭环训练 loop

**Spec 1 要解决的问题**：现状 `run_replay` 同步阻塞，全市场回测（`scripts/calibrate_min_rr.py` 实证「几十分钟~几小时」）HTTP 必超时。Spec 1 把全市场回测异步化，使其可执行、可观测进度、可取消、结果可持久化——这是闭环训练的物理前提。

**现状基线**：
- ✅ 全市场回测能力已有：`backtest_replay.replay(universe=None)` 按 `reader.symbols` 枚举。
- ⚠️ Celery 壳在（`server/celery_app.py` + Redis），但只承载实盘 beat（scan/monitor 三任务），未包回测 task；且有 P1-9b follow-up「想去 Celery、迁 APScheduler」。
- ✅ GLM AI 通道已有（review 端点）。
- ✅ 方案A归档已落地（`caisen/replay_runs.py`，JSON 文件）。
- ❌ 回测未异步化；项目至今零 DB（无 sqlite3/sqlalchemy/CREATE TABLE 用法）。

## 2. 设计决策（brainstorm Q&A 结论）

| 决策点 | 选定 | 理由 |
|---|---|---|
| scope | 单回测异步化（不含参数扫描批处理） | YAGNI；扫描编排是 Spec 4 的事 |
| 任务状态持久化 | 独立任务表（全生命周期） | 失败/取消也是实验数据，必须留痕 |
| 存储 | SQLite 单一真相源（迁 replay_runs）+ 标准库 sqlite3 | 既然引入 DB 就做单一源，避免两套存储割裂；sqlite3 无新依赖 |
| 进程模型 | 自管 ProcessPoolExecutor 寄生 uvicorn，concurrency=1 串行 | 合「零守护进程」哲学、与 P1-9b 去 Celery 同向；回测主动触发、寄生风险可控 |
| 回测核心改造 | replay 加 progress_cb/abort_cb（可选、向后兼容） | 几十分钟任务无进度无取消=不可用 |

## 3. 架构

### 3.1 进程模型
```
uvicorn 进程
 ├─ lifespan 起 ProcessPoolExecutor(max_workers=1) —— 回测 worker 子进程
 │   └─ initializer: DataLakeReader.load(daily) 一次（数 GB 常驻复用，不每次重 load）
 ├─ API 端点 —— 只读写 SQLite，不碰回测
 └─ daemon 线程「调度器」: poll SQLite PENDING → submit worker（concurrency=1，满则 FIFO 排队）
```

### 3.2 数据流
```
POST /replay/async → 写 SQLite(PENDING) → 调度器 poll 到 → submit worker
worker: 标 RUNNING → 跑 replay(progress_cb/abort_cb)
      → 完成 SUCCESS+report_json / 异常 FAILED+error / abort CANCELLED
进度/abort 经 multiprocessing.Queue 传，主进程单点写 SQLite（避免跨进程 DB 锁）
worker 周期写 last_heartbeat，调度器据此识别崩溃
```

### 3.3 重启恢复
uvicorn 启动时：`UPDATE replay_tasks SET status='FAILED', error='进程重启中断' WHERE status='RUNNING'`（崩溃/重启残留的 RUNNING 标 FAILED，不自动重跑——由用户决定是否重新提交，避免无意识重复消耗几十分钟~几小时算力）。与 §7「worker 崩溃标 FAILED 不重跑」统一语义：任务被打断=FAILED，不自动重跑。

## 4. 存储模型（SQLite 单一真相源）

### 4.1 schema
```sql
CREATE TABLE replay_tasks (
  task_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,            -- ISO 微秒，排序键
  status TEXT NOT NULL,                -- PENDING/RUNNING/SUCCESS/FAILED/CANCELLED
  progress INTEGER DEFAULT 0,          -- 0-100（已处理 symbol 占比）
  start TEXT, end TEXT,
  universe_n INTEGER,                  -- -1=全市场
  cfg_json TEXT,                       -- cfg_override 快照
  error TEXT,                          -- FAILED 错误信息
  report_json TEXT,                    -- SUCCESS 内嵌完整 ReplayReport
  started_at TEXT, finished_at TEXT, last_heartbeat TEXT
);
CREATE INDEX idx_tasks_status ON replay_tasks(status);
CREATE INDEX idx_tasks_created ON replay_tasks(created_at DESC);
```
DB 文件路径 `data/replay_tasks.db`（与其他数据资产同源）；启用 WAL 模式提升并发读。

### 4.2 迁移 replay_runs（JSON→SQLite）
一次性脚本 `scripts/migrate_replay_runs_to_sqlite.py`：遍历 `replay_runs/*.json` → INSERT 为 SUCCESS 行（report_json 存原 report 全字段）。caf3772 后数据量小，一次跑完。迁移后老 JSON 目录保留只读/归档。

### 4.3 /replay/runs 改读 SQLite
`GET /replay/runs` 端点改 `SELECT ... WHERE status='SUCCESS' ORDER BY created_at DESC`，前端契约（`ReplayRunSummary`）不变。

## 5. 回测核心改造

### 5.1 backtest_replay.replay() 加可选回调（向后兼容）
```python
def replay(price_data, cfg, risk, start, end, aum, *,
           progress_cb=None,   # Callable[[done:int, total:int], None]
           abort_cb=None,      # Callable[[], bool] —— True 即中止
           trading_calendar=None):
```
- symbol 外层循环：每处理完 50 个 symbol 调一次 `progress_cb(done, total)`（全市场 5000 只 ≈ 100 次上报，平衡进度精确度与 SQLite 写频）。
- 双层循环（symbol/T）顶检查 `if abort_cb and abort_cb(): raise ReplayAborted()`。
- 默认 None = 现状行为，不破坏现有 3 个 caller（`__main__` / `calibrate_min_rr` / `caisen_service`）。

### 5.2 新建编排层
- `caisen_service.run_replay_async(req) -> task_id`：生成 task_id → 写 PENDING 行 → 立即返回（不阻塞）。
- `caisen_service.run_replay_worker(task_id)`：worker 进程入口。读 task 行 → 装配 price_data → 跑 replay(带 cb) → 写回 SUCCESS/FAILED。abort flag 经 Queue 传入。

## 6. API 端点

| 端点 | 方法 | 作用 |
|---|---|---|
| `/replay/async` | POST | 提交回测 → 写 PENDING → 返 task_id（立即返回） |
| `/replay/tasks/{id}` | GET | 单任务状态/进度/结果 |
| `/replay/tasks` | GET | 任务列表（降序，可按 status 过滤） |
| `/replay/tasks/{id}/cancel` | POST | 取消（置 abort flag） |
| `/replay/runs` | GET | 改读 SQLite SUCCESS（契约不变） |

**老同步 `POST /replay`**：**废弃** —— 统一走 async，小样本秒级任务也走队列，免维护两套入口。

## 7. 边界与错误处理
- **worker 崩溃**：last_heartbeat 超时（如 5min 无更新）→ 调度器标 FAILED，不自动重跑（重跑由用户决定，避免无意识重复消耗算力）。
- **data_lake 离线**：worker 装配 price_data 空 → 写 FAILED + 错误信息（不卡死）。
- **取消竞态**：cancel 置 flag，但任务在 abort_cb 检查点之间已完成 → 以实际结果为准（SUCCESS）。
- **并发提交**：concurrency=1，多余任务 PENDING 排队，FIFO。
- **SQLite 并发**：主进程单点写（worker 经 Queue 上报），WAL 模式读不阻塞写。

## 8. 测试策略
- task 状态机全路径：PENDING→RUNNING→SUCCESS / FAILED / CANCELLED。
- progress_cb 上报、abort_cb 取消命中。
- 重启恢复（RUNNING→FAILED，不自动重跑）。
- 迁移脚本正确性（JSON→SQLite 字段无损）。
- data_lake 离线降级（FAILED 不卡死）。
- 向后兼容（replay 默认 cb=None，现有 3 个 caller 不破坏）。

## 9. 已确认决策（用户 2026-07-13 拍板）
- 老同步 `POST /replay`：**废弃**（统一走 async）。
- SQLite 文件路径：**`data/replay_tasks.db`**。
- worker 崩溃恢复：**标 FAILED，不自动重跑**（用户决定重提）。
- 进度上报粒度：**每 50 个 symbol 一次**。

本 spec 无遗留待确认项，可进入实现计划。

---

状态：用户已认可（2026-07-13）→ commit → 转 writing-plans 分解实现任务。
