# 数据单一真相源清单（Data Source of Truth）

> 治理日期：2026-08-03。目的：消灭「同一逻辑数据存在多份来源、读写各指各的」的
> 历史债务（典型事故：策略播报读 `replay_runs/index.json` 旧归档，而回测结果早已
> 迁入 SQLite，导致「近期回测」半个月不更新）。

## 总原则

1. **每个数据域只有一个写入口（单一真相源，SSoT）**；其余路径只允许「只读归档」或
   「审计镜像」。
2. 新代码一律读写 SSoT；发现新双源立即在下方表格登记并治理。
3. 允许的双写模式仅限：**DB 真相源 + 审计/展示镜像**（如成交 CSV、计划 JSON），
   镜像必须可从真相源重建，且由同一事务/同一调用点写。
4. 遗留文件统一归档到 `logs/archive/`（只移动不删除，可恢复）。

## 数据域清单

| # | 数据域 | 单一真相源（SSoT） | 遗留/镜像路径 | 状态 |
|---|--------|--------------------|---------------|------|
| 1 | 回测任务与结果 | `data/replay_tasks.db`（`backtest/tasks_db.py`，worker 落 `report_json`） | `replay_runs/*.json` + `index.json`（历史归档，已全量迁移，见 `replay_runs/README.md`）；`backtest/runs.py` 为遗留模块 | ✅ 已治理 |
| 2 | 交易计划 | `logs/trading_plans/plan_<date>.json`（`trading/trading_plan.py`，EOD 写、pre_open/veto 读、钉钉推送） | `plans/<date>.json` 旧 scan 格式（已停用，文件已归档）；`trading_state.db` 的 `trade_event(SIGNAL/CONFIRMED)` 为 DB 镜像（by design 双写） | ✅ 已治理 |
| 3 | 成交流水（审计） | `logs/live_trades.csv`（`trading_service.record_live_trade` 单点写） | `presentation/logs/live_trades.csv`（旧路径，已归档）；仓库根旧 CSV（测试污染，已清理归档） | ✅ 已治理（需重启旧 server 生效，见下） |
| 4 | 持仓/成交账本 | `logs/trading_state.db`（`position/fill/order/trade_event`，`state_store`+`position_book` 共用） | 券商 QMT 真实持仓（对账 reconcile 用，by design）；CSV 审计镜像 | ✅ by design |
| 5 | 作业台账 | `logs/trading_job_run.db`（`job_ledger`，独立库不与 trading_state 混） | — | ✅ by design |
| 6 | 实验版本/审计 | `experiment/experiments.db`（`experiment/store.py`） | — | ✅ 已治理（docstring 陈旧注释已修正） |
| 7 | 参数迭代状态 | `logs/param_iter_state.json`（`discovery/tools/param_iter.py` 写、策略播报读） | — | ✅ 单源 |
| 8 | 参数搜索 trial | `logs/discovery_trials.db`（discovery L4 daemon） | — | ✅ 单源（与 #7 是两个独立实验系统，勿混） |
| 9 | 数据健康度 | 双口径 by design：`data_service`（parquet mtime + 哨兵）与 `trading_state.data_ready`（检查点内容校验） | — | ✅ by design |
| 10 | 应用日志 | `logs/quanter.log`（`LOG_CONFIG`） | `presentation/logs/quanter.log`（旧路径，已归档） | ✅ 已治理 |
| 11 | 播报幂等 | `logs/.last_<bot>_brief`（每 bot 独立） | — | ✅ 单源 |
| 12 | symbol→名称映射 | 双源同源（Tushare）：`data_lake/stock_basic.parquet`（同步落盘，`trading_plan` 用）vs `data/symbol_names`（启动实时拉取，/plans 用） | — | ⚠️ 待统一（建议统一读 parquet，symbol_names 复用同文件） |
| 13 | 前端回测/计划客户端 | — | `presentation/web/src/api/caisen.ts` 的 `listPlans/listReplayTasks/getChart` 指向已下线后端路由（`/api/v1/caisen/*` 已移除） | ⚠️ 待治理（死代码；首页 `/caisen` 会 404 空态） |
| 14 | 环境变量 | `.env`（`load_dotenv(override=True)` 单一真相源） | 系统环境变量（会被 .env 覆盖） | ✅ 单源 |

## 本次治理动作（2026-08-03）

1. **回测结果统一到 SQLite**：`ops/migrate_replay_runs_to_sqlite.py` 把遗留 JSON
   归档（`20260712-*`、`20260714-*`）全量迁入 `data/replay_tasks.db`（幂等，共 8 条
   任务）；播报已改读 SQLite（JSON 仅作只读回退）。
2. **遗留文件归档**：`presentation/logs/quanter.log`、`presentation/logs/live_trades.csv`、
   CSV 备份、`plans/2024-06-01.json` + `.lock` → `logs/archive/`（只移动不删除）。
3. **遗留目录标记**：`replay_runs/README.md` 声明只读归档；`backtest/runs.py` 标注
   「遗留只读模块，生产禁止写入」。
4. **陈旧注释修正**：`broadcast/brief_strategy.py`、`broadcast/__main__.py`、
   `experiment/store.py` 中指向已停用 `plans/<date>.json` / `replay_runs/index.json`
   的注释全部更新。

## 遗留风险与下一步

- **运行中的旧 server**（2026-08-03 00:44 启动）仍以旧 `PROJECT_ROOT` 解析
  `presentation/logs/`；重启后才会完全切到新路径（期间若产生成交流水会写回旧路径，
  需在重启后再次检查归档）。
- **前端 `/caisen` 死端点**：后端 caisen 路由已下线但前端 API 客户端仍在调用
  （首页会报错/空态）；建议后续从 `caisen.ts` 移除死函数或恢复只读后端。
- **名称映射双源**（#12）：内容同源（Tushare），但落盘 parquet 与实时拉取可能漂移；
  建议统一以 `data_lake/stock_basic.parquet` 为 SSoT，`symbol_names` 改读同文件。
