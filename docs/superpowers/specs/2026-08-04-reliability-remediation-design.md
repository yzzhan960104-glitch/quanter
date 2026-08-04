# 全链路可靠性整改：数据观测层断链 + 交易执行 Live P0 + 架构治理

- **日期**：2026-08-04
- **分支**：master @ 0459639f（spec 阶段）
- **状态**：待审（spec review gate）
- **关联**：
  - C-7 spec/plan（start_all 收编 + schtasks ONSTART + discovery 收编 lifespan）
  - C-8 spec（启动补跑 + job 台账——本次 A2/B4 复用它）
  - C-2 spec（pipeline_then_eod 事件链 + 播报收口）
  - C-4 spec（L1 停调度 / 熔断 / 冷启动语义）
  - trading-gap4（撤单 CANCELLED 状态撒谎，live P0 既有清单）
- **范围**：四路整改 = A 数据观测与调度治理 / B 交易执行风控闭环（Live P0）/ C 架构拆分 / D 安全与工程治理。本次只落 spec；实施按 workstream 拆 plan。
- **来源**：三份独立审计合并——(1) 本会话全项目梳理；(2) 用户对梳理的三处订正 + 两处复发隐患；(3) 外部 sub-agent 审读（对 HEAD 0459639f，含 4 个系统性根因 + 6 项 live P0）。所有论断均已在本会话逐条代码实锤。

---

## 1. 背景与现状

### 1.1 痛点

**数据观测层（生产已断）**
1. `quanter_sync_incremental` 计划任务指向 Desktop 旧路径（`C:\Users\yzzhan\Desktop\quanter\scripts\run_sync_incremental.bat`，文件不存在）→ 07-24 起每天 18:00 失败（Last Result=1），最后成功 07-23 18:00 → moneyflow/margin/ths_daily/share_float/suspend_d/top_inst/margin_detail/moneyflow_hsgt 等 quick 批数据集停更 271h。
2. data_service 同步子进程不拼 key：`_run_sync_subprocess` 的 cmd = `[sys.executable, script_abs, *args]`，registry 全部未配 args → `data/tools/sync_tushare.py`（要求位置参数 key）必报 usage 退出码 2 → 每次 API/前端/sweep 触发都写 `.failed` 哨兵。
3. `.failed` 哨兵两批历史残留（`data_lake/.syncing/`）：
   - 502B 批（19 个，07-21/07-24）：内容为 argparse 缺 key 报错的截断尾部——usage/choices 列表尾部 + `sync_tushare.py: error: the following arguments are required: key`（error 行在文件末尾，_ERR_TAIL_CHARS=502 保留）；
   - 178B 批（7 个，07-26/07-28）：内容为 `can't open file 'F:\quanter\presentation\data\tools\sync_tushare.py'`（旧版 cwd 错位 `presentation/` + 系统 Python；当前代码已用 `_PROJECT_ROOT` 锚定 + `sys.executable` + `cwd=_PROJECT_ROOT`，不会再生成）。
4. `_derive_status`（data_service.py:94）状态机：`.failed` 存在即返 failed，**不看 parquet mtime** → daily/index_daily 数据已新仍显示 failed（哨兵压倒 mtime）。
5. sweep 只重试 stale/missing，不重试 failed → 失败数据集永不自动恢复（设计取舍，见 A1.3）。
6. 前端业务日期用 `toISOString()`（UTC）：JobCockpitView.vue:34、TradesTable.vue:63、LiveCockpitView.vue:97 → 北京凌晨 0-8 点取到"昨天"。

**交易执行层（Live P0，动真钱）**
7. 撤单 CANCELLED 状态撒谎（三模块串）：`broker/qmt.py:990` 撤单指令发出即返 `OrderState.CANCELLED`（message 自称非终态但状态值即终态）；`trading/io/breaker.py:124` 在确认前就把 DB 回写 CANCELLED；`trading/state_store.py:413` 按 broker_oid 落 CANCELLED。QMT 主推延迟 1-2s 窗口内，消费方据终态释放敞口 slot → 重复废单。
8. auto_publish 护栏 fail-open：`research/discovery_bridge.py:120-121` `except Exception: return None` → 异常被当"无 ACTIVE 门槛" → 0% 垃圾冠军照样 publish DRAFT；且 `auto_publish_champion(db_path=...)` 调 `_active_outer_ann()` 未透传 db_path（实证 2026-08-04 误建 DRAFT neckline_prop_20260804_48662a）。
9. 部分成交精度未量化：`trading/position_book.py:166-206` 浮点裸累加，无 NaN/零价/负数/小数位防护；`broker/base.py:46 OrderResult.filled_qty=0.0 / avg_price=None` 恒默认，`broker/qmt.py` 从不回填 → 实盘部分成交后账本与柜台口径分歧 → reconcile 误报 drift。
10. 熔断冷启动 fail-open：`trading/engine.py:1652` start_equity 无基线（None 或 ≤0）→ `breaker_skipped=True` + `logger.warning` 软降级跳过当日日内熔断（不阻断开仓）→ 冷启动日无熔断保护（fail-open 判断本身正确，"静默"不准确，实为 WARN + 观测标记位）。
11. 交易计划非原子写：`trading/trading_plan.py:41` `p.write_text(...)` 直写 → 崩溃窗口落半截 JSON → `load_plan` 返 None → pre_open 静默跳过挂单。

**架构与安全**
12. `trading/engine.py` 3223 行巨石：五条生命周期 + 状态机 + health + 成交 handler 全揉一文件；`trading/__init__.py` 已有拆分蓝图未落地。
13. `discovery/objective.py:62` calmar=+Inf（max_dd≈0 且 ann>0）进排序（`discovery/cli.py:231` reverse=True）→ +Inf 垄断 top，伪收敛元凶之一。
14. `infra/tools/dingtalk_review_bridge.py` docstring 含 unified-app-id 真值，已进 git history（安全）。
15. GBK emoji 崩溃（stdout 被管道/重定向时 cp936 → UnicodeEncodeError）：`ops/data_pipeline.py`、`ops/brief_all.py`（⚠️ 行）、`trading/tools/qmt_live_smoke*.py`、`trigger_eod_once.py`、`trigger_pre_open_once.py`、`smoke_trading_engine.py`、`qmt_reconcile_positions.py`、`compute_unit/*`、`discovery/tools/param_iter.py`、`scripts/export_sobol_task.py`、`data/tools/probe_tushare_fields.py`、`scan_integrity.py` 等约 13 文件。
16. `trading/tools/qmt_smoke.py:96` 硬编码 `price=5.0` 涨停价买单（同目录 realorder 有正确实现未回填）。
17. `discovery/schtasks.py --register` 可重建已退役的 `QuanterDiscoveryDaemon` → 与 lifespan cron 02:00 双跑；`ops/manage_ops_schtasks.py:35 RETIRED_TASKS` 缺 `QuanterDailyBrief`。

### 1.2 现状（master HEAD 0459639f）

| 域 | 现状 | 证据 |
|---|---|---|
| 计划任务 | 仅剩 2 个 Quanter 任务：`QuanterServer`（Interactive only）、`quanter_sync_incremental`（Desktop 旧路径，每天失败） | `Get-ScheduledTask` / `schtasks /V` |
| 同步入口 | 日频 quick 批走 `run_sync_incremental.bat`（断）；服务器端走 `sync_tushare.py`（缺 key，必败）；daily 走 `sync_daily_incremental.py`（正常） | `.syncing/*.failed` / quanter.log 07-24 |
| 哨兵状态机 | `.failed` 存在 → failed；sweep 只重试 stale/missing | data_service.py:94-122 / :279 |
| 撤单状态 | 指令发出 = CANCELLED 终态；breaker 确认前回写 DB | qmt.py:990 / breaker.py:124 / state_store.py:413 |
| 护栏 | auto_publish 异常 → None → 放行 | discovery_bridge.py:120-121 |
| 成交精度 | position_book 无校验；OrderResult 成交字段恒空 | position_book.py:166 / broker/base.py:56 |
| 熔断基线 | 无基线跳过熔断（fail-open） | engine.py:1652 |
| 计划落盘 | 直写非原子 | trading_plan.py:41 |
| 文件规模 | engine.py 3223 行 | wc |

### 1.3 本会话已完成的修复（基线内，不再重复入 plan）

| 修复 | 位置 | 验证 |
|---|---|---|
| `_sync_by_symbol` 增量合并（非复权拉缺口合并；复权重拉全区间保 qfq 基线） | data/tushare_sync.py | 81 测试 + 真实 index_daily 增量验证 |
| connect_manager 存活检测弃 tasklist 改 ctypes（64 位句柄签名） | broadcast/connect_manager.py | 13 测试；status 1.3s 准确 |
| brief_all 汇总行 GBK emoji 已修；⚠️ 残留两处（:62 run_brief_all 失败路径 / :74 main 失败路径，见 A6） | ops/brief_all.py:62/:74 | 12 测试 |
| index_daily 数据补到 08-03、周一播报补推 | data_lake / logs | .last_*_brief=08-03 |
| 僵尸任务/快捷方式清理（QuanterDiscoveryDaemon、QuanterDailyBrief、QuanterStartAll.lnk） | 系统 | schtasks 仅剩 2 项 |

---

## 2. 目标与非目标

### 目标

**A 数据观测与调度治理**
1. 恢复 quick 批日频同步（moneyflow/margin/ths_daily/share_float/suspend_d/top_inst/margin_detail/moneyflow_hsgt 等）到最新交易日；前端状态机真实反映健康度。
2. 根治服务器端同步入口：`_run_sync_subprocess` 把 key 拼进 cmd（对 `sync_tushare.py`）；registry 与脚本参数契约显式化。
3. 哨兵状态机闭环：成功路径统一清 `.failed`；历史两批哨兵一次清理；unavailable 数据集前端单列"不可用"不参与健康告警。
4. 计划任务治理：`quanter_sync_incremental` 改指仓库 bat；`QuanterServer`/`quanter_sync_incremental` 改 Password 登录模式；`discovery.schtasks` 标记退役（register 拒绝）；`RETIRED_TASKS` 补 `QuanterDailyBrief`。
5. 数据质量：qfq 除权后历史基准全量重算（不再 follow-up 挂账）；`_sync_by_date` 对损坏/空 shard 增加覆盖校验。
6. 日期口径统一：前端业务日期全部改本地时区。
7. GBK 输出治理：运维/工具入口统一 UTF-8（stdout reconfigure 或 bat `PYTHONUTF8=1`），消灭 emoji 崩溃。

**B 交易执行风控闭环（Live P0）**
8. 撤单状态机：新增非终态表示（`PENDING_CANCEL` 或 OrderResult.confirmed 标志）；breaker/engine 统一"确认后才落 DB 终态/释放 slot"；QMT 主推延迟窗口内不误释放敞口。
9. auto_publish 护栏 fail-closed：异常/查询失败 → 不 publish + CRITICAL 告警；`db_path` 全程透传。
10. 部分成交精度：position_book 入账校验（NaN/零价/负数/小数位）；`OrderResult.filled_qty/avg_price` 由 on_stock_order 回填；reconcile 对空值显式分支。
11. 熔断冷启动：无基线时默认禁止开新仓（或 CRITICAL + 人工放行），不静默跳过。
12. 计划落盘原子化：临时文件 + `os.replace`，写前 JSON 校验。

**C 架构拆分**
13. engine.py 按 `trading/__init__.py` 蓝图拆五模块（scheduler 装配 / 生命周期 / health / 成交 handler / 熔断策略），行为零漂移（golden + smoke 验证）。
14. discovery 伪收敛治理：calmar=+Inf 在排序/前沿处显式处理（+Inf 不参与 top 判定或按 ann 次级规则）。
15. `qmt_smoke.py` 硬编码价格回填为真实报价逻辑（复用 realorder 实现）。

**D 安全与工程治理**
16. unified-app-id 视为已泄露：轮换 + git history 清理（filter-repo）+ 文档去真值。
17. CI/测试加固：GBK 管道输出冒烟、撤单状态机测试、原子写测试、护栏 fail-closed 测试。
18. 僵尸资产清理：orphan bat、`_render_pdf.py` Desktop 路径、registry lake 漂移、archive 文档标注。

### 非目标（显式 out of scope）
- **不改策略/信号逻辑**（颈线法、实验体系语义不动）。
- **不重建已退役 schtasks**（QuanterDataPipeline/QuanterBrief/QuanterDiscoveryDaemon/QuanterDailyBrief 一律只清不退）。
- **不做逐日历史回补**（政策 A：只补最近一致态；采集增量天然回填）。
- **不改交易策略引擎的决策入口**（pre_open/eod 挂单语义不变，只改落盘/确认/护栏机制）。
- **不引入新依赖**（原子写/存活检测/UTF-8 均用 stdlib）。
- **不重写前端**（只修日期口径与状态展示）。

---

## 3. 架构

### A 数据观测与调度治理

#### A1 服务器端同步入口修复（根治）

`presentation/server/services/data_service.py:_run_sync_subprocess`：
- 当前：`args = list(spec.get("args", []))`；`cmd = [sys.executable, script_abs, *args]`。
- 改为：仅当 `os.path.basename(script_rel) == "sync_tushare.py"` 时 `cmd = [sys.executable, script_abs, key, *args]`；`sync_macro_credit.py` 保持原样（其 `__main__` 忽略 argv，已验证无 argparse）。
- 同步在 registry 层固化契约：`DATASET_REGISTRY[key]["script"]` 是唯一事实源；后续新增数据集必须配套"脚本 + args 契约"（spec review 检查点）。

#### A2 哨兵状态机闭环

- `_run_sync_subprocess` 成功路径已 `_clear_sentinel`（现状保留）；补：**同步入口（含 CLI `data.sync`/`sync_incremental`）成功落盘后不再依赖 data_service 代清**——新增 `_clear_sentinel` 的幂等调用点由 A1 子进程成功返回覆盖（服务器端触发时天然清除）。
- 一次性清理：`data_lake/.syncing/*.failed` 两批（502B 19 个 + 178B 7 个）由运维脚本删；178B 批不会再生（cwd 已锚定）。
- `_derive_status` 增加 unavailable 分支：`TUSHARE_DATASETS[key].get("_unavailable")` → 返回 `"unavailable"`（新状态），前端单列不告警；`list_datasets` 透传。
- sweep 政策明确（不自动重试 failed）：failed = 真实同步失败（配额/网络），防自动重试烧配额；保留前端手动触发 + 启动后 stale/missing 自动补。

#### A3 计划任务治理

- `quanter_sync_incremental`：`schtasks /Change /TN quanter_sync_incremental /TR "F:\quanter\scripts\run_sync_incremental.bat"`。
- `QuanterServer` + `quanter_sync_incremental`：改 Password 登录模式（GUI 或 `schtasks /Create ... /RP`；手册已在会话内交付）。
- `discovery/schtasks.py`：模块 docstring + `--register` 改为显式拒绝（打印"已退役，收编 lifespan，勿注册"），仅保留 `--unregister/--query`。
- `ops/manage_ops_schtasks.py:35`：`RETIRED_TASKS` 追加 `"QuanterDailyBrief"`（幂等清退，防复活）。

#### A4 数据质量

- **qfq 除权重算**（`data/tools/sync_daily_incremental.py` ④ 除权检测命中时）：不再只告警；对 `div_syms` 走 `data.sync --keys daily --no-resume --since <历史起点>`（复用 `_sync_by_symbol` 的 adj 全区间重建路径，已具备）或调用 `sync_daily_incremental --full-recompute` 新开关，重建前复权基准后合并落盘；验收：300214 类除权标的历史 qfq 与 `fetch_qfq` 口径逐位一致。
- **`_sync_by_date` shard 校验**：`resume=True` 且 shard 存在时读行数与日期覆盖；空/损坏（行数 0 或日期列解析失败）→ 视为缺失重拉；新增单测。
- **前端日期**：三处 `toISOString().slice(0,10)` 改本地时区格式化（`dayjs` 或手写 `getFullYear/getMonth/getDate`）；新增 UTC+8 凌晨用例。

#### A5 GBK 输出治理

- 统一策略：运维/工具 CLI 入口（`ops/*`、`trading/tools/*`、`data/tools/probe_tushare_fields.py`、`scan_integrity.py`、`compute_unit/*`、`discovery/tools/param_iter.py`、`scripts/export_sobol_task.py`、`infra/tools/dingtalk_review_bridge.py` 已自带）在 `__main__` 加：
  ```python
  try:
      sys.stdout.reconfigure(encoding="utf-8", errors="replace")
  except Exception:
      pass
  ```
- 或等价：相关 `.bat` 加 `set PYTHONUTF8=1`（`start_server.bat` 已有；`run_sync_incremental.bat` 等补齐）。
- 修 `ops/brief_all.py:62`（run_brief_all）与 `:74`（main）两处残留 `⚠️`。
- 验收：在 cp936 管道下逐入口冒烟（PowerShell `| Out-File` + pytest subprocess 断言不抛 UnicodeEncodeError）。

### B 交易执行风控闭环（Live P0）

#### B1 撤单状态机（三模块串联修复）

- `broker/base.py`：`OrderState` 新增 `PENDING_CANCEL`（或 OrderResult 增加 `confirmed: bool`；推荐显式中间态，语义最清晰）。`cancel_order_stock` rc==0 返 `PENDING_CANCEL`（不再返 CANCELLED），message 保留现状说明。
- `trading/io/breaker.py:117-133`：只对 `confirm_fn` 确认成功的单回写 DB CANCELLED；未确认（超时/False）→ 不写终态，记 `n_unconfirmed` + WARNING 人工复核；`cancel_order_by_broker_oid_db` 调用移到确认之后。
- `trading/state_store.py:413`：`cancel_order_by_broker_oid_db` 语义不变（仍按柜台单号回写），但调用点受 B1 约束；新增 `cancel_order_pending`（写 PENDING_CANCEL）供未确认路径。
- `trading/engine.py` 所有撤单消费点（pending 撤单 1299、max_holding、熔断善后）：统一 `gw._confirm_cancelled` 为唯一终态判定；`PENDING_CANCEL` 不计成功、不释放敞口 slot。
- 验收测试：
  - `test_cancel_unconfirmed_keeps_slot`（fake gw 延迟 2s 不确认 → slot 不释放、DB 非 CANCELLED）；
  - `test_breaker_writes_cancelled_only_after_confirm`；
  - 实盘 smoke：模拟盘撤单，2s 窗口内状态为 PENDING_CANCEL，确认后 CANCELLED。

#### B2 auto_publish fail-closed

- `research/discovery_bridge.py:_active_outer_ann`：`except Exception` → 记日志 + `raise` 或返回哨兵值（推荐 `(None, err)` 双返回值）；`auto_publish_champion` 捕获后 **不 publish** + `infra.notifier` CRITICAL 告警。
- `db_path` 透传：`auto_publish_champion(trial_id, outer, db_path)` → `_active_outer_ann(db_path=db_path)`。
- 验收：mock `list_versions` 抛 TypeError → 断言不建 DRAFT + 告警发出（回归 2026-08-04 事故场景）。

#### B3 部分成交精度

- `trading/position_book.py:apply_fill` 入口校验：`qty`/`price` 非 NaN、非负、`qty>0`；小数位按标的精度（A 股 100 股整数 + 2 位价格）告警拒收；新增 `validate_fill` 纯函数。
- `broker/qmt.py` 成交回调（on_stock_order / async_response）：回填 `OrderResult.filled_qty/avg_price`（或直接以回调 payload 为准落账，绕开 OrderResult 空字段）。
- `trading/reconcile_job.py`：本地账本空/未知时显式分支（不误报 drift），对账报告区分"无本地记录"与"数量分歧"。
- 验收：mock 部分成交（1000 股挂单成交 300 股）→ 账本 qty/avg 正确、reconcile 不误报；NaN 价拒收测试。

#### B4 熔断冷启动

- `trading/engine.py:1652`：无基线时不再静默跳过——默认 fail-closed（`post_close` 熔断检查置"无法评估" → CRITICAL + 当日禁止开新仓开关）；提供 env `BREAKER_NO_BASELINE=warn|block`（缺省 block）供灰度。
- 验收：`get_start_equity` 返 None → 断言 block 行为 + CRITICAL；有基线 → 行为零变化。

#### B5 计划原子写

- `trading/trading_plan.py:save_plan`：写 `plan_<date>.json.tmp`（同目录）→ `json.dumps` 校验 → `os.replace`；异常时清理 tmp。
- `load_plan` 损坏语义不变（返 None 不漏挂脏单），补损坏日志。
- 验收：mock `write_text` 中断 → 旧文件完好；并发读写测试。

### C 架构拆分

#### C1 engine.py 拆分（按 `trading/__init__.py` 蓝图）

- 目标结构：
  - `trading/engine.py`：TradingEngine 门面（装配 + 启动/关停 + 对外 API），行数目标 <600；
  - `trading/lifecycle.py`：lifespan 生命周期（pre_open/eod/post_close 编排）；
  - `trading/scheduler.py`：APScheduler 四 cron + interval 装配（自 `__init__` 迁出）；
  - `trading/health.py`：_health_guard / 心跳 / 熔断状态；
  - `trading/trade_handlers.py`：成交/撤单/对账 handler（含 B1 消费点）；
  - `trading/breaker_policy.py`：熔断策略（含 B4）。
- 约束：纯搬移 + 最小接口调整；`_critical_guard`/台账/时钟语义零变化；golden（颈线法基线）+ `tests/trading` 全绿 + 实盘 smoke 零漂移。
- 验收：`pytest tests/trading tests/test_caisen_replay_runs.py -q` 全绿；`trading/engine.py` 行数断言 <600（可选 lint）。

#### C2 discovery 伪收敛治理

- `discovery/objective.py`：`calmar=inf` 时输出 `None` 或超大有限哨兵（如 1e9）并在 `discovery/cli.py:231` 排序键处显式过滤/次级规则（+Inf 不垄断 top；同等 ann 下比样本量/回撤）。
- `trading/tools/qmt_smoke.py:96`：price=5.0 改为读实时行情（复用 `qmt_live_smoke_realorder.py` 的报价逻辑）；保持 dry_run 安全。

### D 安全与工程治理

- **密钥**：`REVIEW_BOT_UNIFIED_APP_ID` 对应 app 视为泄露 → 钉钉侧轮换；`git filter-repo` 清理 `infra/tools/dingtalk_review_bridge.py` 文档真值 + 历史；文档改 `<REVIEW_BOT_UNIFIED_APP_ID>` 占位符。
- **CI/测试**：新增 `tests/ops/test_gbk_stdout_smoke.py`（cp936 管道跑入口脚本断言不崩）；B1/B2/B3/B5 单测如各节；`pytest tests/ -q` 回归门禁。
- **僵尸资产**：orphan bat 12 个移入 `scripts/archive/`（或标注历史元数据）；`ops/_render_pdf.py` Desktop 路径改 F:\quanter 或删除；registry `hsgt_top10/top_list` lake 配置与 `LAKE_CONFIG` 对齐（unavailable 项建议从 TUSHARE_DATASETS 移除 lake 或补注册）；archive 文档头部加"历史，路径已迁 F:\quanter"。

---

## 4. 分阶段实施顺序

| Phase | 内容 | 依赖 | 验收 |
|---|---|---|---|
| 0 止血（半天） | A1 任务路径 + A2 拼 key + 补跑 quick 批 + 清两批哨兵 | 无 | `/datasets` 除 unavailable 外全绿；lag 归零 |
| 1 交易风控（1-2 周） | B1→B2→B3→B4→B5 | 无（与 0 并行） | 各节单测 + 实盘 smoke（模拟盘撤单窗口） |
| 2 数据质量（1 周） | A4 qfq 重算 + shard 校验 + 前端日期 + A5 GBK + A3 任务治理 | 0 | 300214 类标的逐位一致；cp936 冒烟过 |
| 3 架构（2-3 周） | C1 engine 拆分 + C2 伪收敛/qmt_smoke | 1（B1 消费点先固化） | golden + tests/trading 全绿；engine <600 行 |
| 4 安全治理（1 周） | D 密钥轮换 + git 清史 + CI + 僵尸资产 | 任意 | filter-repo 后 grep 无真值；CI 绿 |

---

## 5. 风险与回滚

- **B1 状态机改动**：影响撤单/熔断/对账三处消费点——每步单独提交 + 模拟盘 smoke；回滚 = revert 单提交（状态枚举新增向后兼容，旧 CANCELLED 数据读取兼容）。
- **A2 registry args 契约**：新增数据集未配 args 会在 review 拦截；回滚 = 还原 data_service 一行。
- **C1 拆分**：纯搬移但风险在隐式依赖——先跑 golden 再合；回滚 = revert 大提交（文件级）。
- **密钥轮换**：轮换期间 review 桥短暂不可用（计划窗口内完成）。
- **数据补跑**：快速批拉取耗 Tushare 配额——限频/熔断已有；失败留 failed 不自动重试（政策 A）。

---

## 6. 附录：问题总账（合并三份审计）

| 优先级 | 问题 | 归属 |
|---|---|---|
| Live P0 | 撤单 CANCELLED 撒谎（qmt/breaker/state_store） | B1 |
| Live P0 | auto_publish fail-open + db_path 不透传 | B2 |
| Live P0 | 部分成交精度缺失（position_book/OrderResult） | B3 |
| Live P0 | 熔断冷启动 fail-open | B4 |
| Live P0 | 计划非原子写 | B5 |
| P1 | quanter_sync_incremental Desktop 路径 | A3 |
| P1 | data_service 缺 key 必败 | A1 |
| P1 | .failed 两批哨兵 + 状态机 | A2 |
| P1 | Interactive only ×2 | A3 |
| P1 | discovery/schtasks 可复活双跑 | A3 |
| P1 | RETIRED_TASKS 缺 QuanterDailyBrief | A3 |
| P1 | qfq 除权不重算 | A4 |
| P1 | 前端 UTC 日期 ×3 | A4 |
| P1 | GBK emoji 崩溃 ~13 文件 | A5 |
| P1 | unified-app-id 泄漏 git history | D |
| P2 | engine.py 3223 行巨石 | C1 |
| P2 | calmar +Inf 垄断排序 | C2 |
| P2 | qmt_smoke price=5.0 | C2 |
| P2 | orphan bat / _render_pdf / registry 漂移 / unavailable 前端标记 | D/A2 |
| 已修 | _sync_by_symbol 增量 / connect_manager 存活检测 / 僵尸任务清理 / index_daily 补数 | 基线 |
