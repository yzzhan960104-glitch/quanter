# 网关重连与单一真相源整改设计（gateway-ssot-hardening）

- **日期**：2026-08-04
- **分支**：master @ 96225961（spec 阶段）
- **状态**：待审（spec review gate）
- **关联**：
  - `docs/superpowers/specs/2026-08-04-reliability-remediation-design.md`（同日早期 spec，基线 0459639f；本 spec 聚焦 08-04 上午新实锤的网关/真相源问题，二者互补，实施时按 workstream 合并排期）
  - `docs/superpowers/specs/2026-08-01-live-mainchain-fixes-design.md`（成交回报主链路修复，本 spec 的 W3 是其后续收口）
  - `docs/data-source-of-truth.md`（单一真相源清单，本 spec 的 W6 需同步修订该文档）
  - commit `1132d51e`（QMT connect -1 根治：bootstrap 就绪 gate + is_client_ready 只认客户端文件）——本 spec W1 证明其就绪信号选错
- **范围**：六路整改 = W1 网关就绪与重连（P0）/ W2 计划生命周期真相源（P0）/ W3 成交流水真相源收敛（P1）/ W4 日权益快照收口（P1）/ W5 数据就绪单口（P1）/ W6 治理与巡检（P2）。本次只落 spec；实施按 workstream 拆 plan。
- **来源**：2026-08-04 上午生产事故链（09:22 pre_open 静默跳过 + 24 笔重复成交回报）逐条代码实锤 + 全项目单一真相源审计。

---

## 1. 背景与现状

### 1.1 触发事件（2026-08-04 上午实录）

1. **09:22 pre_open 静默跳过**：`pre_open gate 未通过：网关未连接，跳过挂单`；台账 `job_run(pre_open, 2026-08-04) = skipped`；计划 `plan_2026-08-04.json` confirmed=True 完好，但 order 表为空——计划正常、执行静默失败。
2. **网关 9+ 小时未重连**：引擎 01:08 启动后 `_health_guard` 每分钟执行成功但从未发起 connect；status 恒 `disconnected`。
3. **24 笔重复成交回报**：08-03 简报「买 24 笔」实为同一笔 `600000.SH BUY 100@10.5`（成交时间 20260801101000）被重放 8 批 × 3 行；state_store fill 表为空（幂等拦截成功），CSV 却逐次追加。
4. **双引擎进程**：系统 Python 与仓库 venv 的多个 `python -m trading` 并存（37168→35736 嵌套父子），端口 8000 与单实例锁由 35736 持有，另一进程仍在运行。

### 1.2 问题清单（本会话实锤，共 15 项）

| # | 问题 | 根因 | 影响 | 严重度 |
|---|---|---|---|---|
| 1 | `is_client_ready()` 恒 False：客户端运行 >5 分钟即“未就绪” | 探针看 `miniqmtShm*Cache*`/`up_queue_win_*`（客户端**启动时生成的一次性共享内存镜像**），非心跳文件；客户端运行期间不刷新 | 引擎从不尝试 connect，网关永久断线 | P0 |
| 2 | `_health_guard` 未就绪分支静默 return | [engine.py:2355] 第④段 `if not ready: return` 无日志、无告警 | 断线原因完全不可见，只能等 pre_open 失败暴露 | P0 |
| 3 | 客户端“进程活着但探针判死”：`quoter\SH` 08:45 仍在更新行情，缓存文件停在 01:01 | 就绪信号选错（见 #1） | 运维误判“客户端没启动”，被迫重启客户端绕探针 | P0 |
| 4 | connect 前不清本 sid 残留队列：`down_queue_win_123459` 75MB | `_cleanup_session_files` 只在 connect 返 -1 后补救（[qmt.py:469]），正常连接前不预防 | 连上瞬间重放旧成交回报 → CSV 重复（#10 复发源） | P0 |
| 5 | 双引擎进程：venv + 系统 Python `-m trading` 并存 | schtasks `QuanterServer` 01:07 拉起后，另有进程/嵌套子进程再起；单实例锁未拦住 | 多写者（CSV/计划/账本）、QMT session 抢连（connect -1 历史根因） | P0 |
| 6 | veto/confirm 只写 JSON 镜像，DB `VETOED` 无写入方 | [veto_plan.py:58] 只改 `plan_<date>.json`；全仓无 `insert_trade_event(VETOED)` | eod_plan 重跑可让被否计划复活；DB 终态镜像与 JSON 必然漂移 | P0 |
| 7 | 环境配置与“模拟盘”认知不符 | `.env`：`AUTO_TRADE_MODE=live` + `AUTO_CONFIRM_PLAN=true` + `TRADE_SHADOW_MIN_DAYS=1`（红线 5） | 全自动真单配置下，唯一人审刹车（veto）还有 #6 缺陷 | P0 |
| 8 | CSV 审计镜像无幂等 | [engine.py:3046] `record_live_trade` 在 `insert_fill` 幂等判定之外无条件追加，返回值被丢弃 | 重放刷 CSV → 简报计数虚高、钉钉轰炸 | P1 |
| 9 | 消费端读 CSV 镜像不去重：简报/导出/复盘 | [broadcast/__main__.py:172] / [trading_service.py:301] / [review_service.py:98] | 用户看到的“买 24 笔”即镜像脏数据 | P1 |
| 10 | post_close 兜底以 CSV 为准重写持仓 | [engine.py:1606] `aggregate_fills_by_symbol` 聚合 CSV 后 diff position_book 并重写 qty | 网关恢复后 24 行 → 幻影 2400 股，止损/止盈基于幻影持仓挂卖单（超卖敞口） | P1 |
| 11 | 简报无法区分“空仓”与“持仓未知” | 断线时 `/positions` 409 → 回退本地账本（空）→ 渲染“当前无持仓” | 观测层把未知当零，误导决策 | P1 |
| 12 | `account_daily` 与 `daily_equity` 断链：期初基线永空 | pre_open 只写 `daily_equity`（[engine.py:798]）；post_close 只写 `account_daily` 收盘（[engine.py:1813]）；`state_store.snapshot_start_equity` 生产路径从不调用 | `daily_pnl` 恒 NULL，盈亏口径无法闭合 | P1 |
| 13 | 数据“就绪”三源无对账 | `data_ready`（内容校验）/ `job_ledger`（pipeline 状态）/ parquet mtime + `.syncing` 哨兵（data_service）各自独立写、独立读 | 三张嘴：台账 done、内容缺、播报 healthy | P1 |
| 14 | `docs/data-source-of-truth.md` 已过期 | #7 param_iter 仍标 SSoT（实际已降级 legacy 回退）；#3 CSV 仍标 SSoT（实际是镜像） | 治理清单误导后续开发 | P2 |
| 15 | 冗余镜像与内存态 | `expired_positions.json`（可重算却落盘）、`_position_attribution`（进程内存，重启丢失）、symbol 名称双源、`.last_<bot>_brief` + job_ledger 双幂等、smoke 工具可写生产 plan 路径 | 数据漂移窗口 + 重启后归因丢失 | P2 |

### 1.3 根因归纳（四类）

1. **就绪信号选错 + 失败静默**（#1/#2/#3）：08-04 凌晨“connect -1 根治”把客户端就绪设为连接前提，但把一次性启动产物当心跳；且就绪失败路径无任何可观测性，形成“永远卡住且无人知晓”。
2. **进程治理失效**（#5）：启动链多入口、单实例锁防君子不防嵌套/双解释器，QMT session 抢连的土壤仍在。
3. **镜像与真相源不同点写**（#6/#8/#10/#12/#15）：CSV、计划 JSON、权益双表、expired JSON 都是“镜像”，但都未遵守 `data-source-of-truth.md` 的“同一事务/同一调用点写”原则。
4. **消费端读镜像且不去重 + 观测语义混淆**（#9/#11/#13）：真相源在 SQLite，消费端仍读 CSV/mtime/台账，且把“未知”渲染成“零”。

### 1.4 与既有 spec 的关系（不重复）

- `2026-08-04-reliability-remediation-design.md`：覆盖数据观测层（sync 断链/哨兵/时区）、撤单 CANCELLED 撒谎、auto_publish 护栏、部分成交精度、熔断冷启动、计划非原子写（本 spec W2 直接引用其 #11 原子写方案，不重写）。
- `2026-08-01-live-mainchain-fixes-design.md`：成交回报主链路（order/fill/position 幂等）。本 spec W3 是它的“审计镜像收口”后续。
- 本 spec 只收 08-04 上午实锤且既有 spec 未覆盖的 15 项。

---

## 2. 目标与非目标

### 目标

1. **W1（P0）网关就绪与重连**：引擎在客户端“进程在 + 已登录”时 1 分钟内自动连上；就绪失败/重连失败全程可观测（WARNING + 限流告警）；connect 前清理本 sid 残留队列；单引擎硬约束生效。
2. **W2（P0）计划生命周期真相源**：`VETOED/CONFIRMED` 双写 DB；eod_plan 重跑不得复活被否计划；计划 JSON 原子写（并入既有 #11）。
3. **W3（P1）成交流水真相源收敛**：CSV 只在 `insert_fill` 首次成功时追加；简报/导出/复盘改读 `state_store.fill`；简报去重 + 三态持仓；post_close 聚合不再以 CSV 为准。
4. **W4（P1）日权益快照收口**：`account_daily` 成为唯一日权益表，`daily_pnl` 闭合；`daily_equity` 降级为兼容读口或删除。
5. **W5（P1）数据就绪单口**：合成单一 `ready` 判定函数，pre_open/catchup/播报统一消费。
6. **W6（P2）治理与巡检**：修订 `data-source-of-truth.md`；清理/迁移冗余镜像；新增漂移巡检脚本（计划↔DB、权益双表、CSV↔fill、引擎进程数）。

### 非目标（显式 out of scope）

- QMT 客户端登录态的自动化（客户端登录是产品约束，引擎不做“自动登录券商”）。
- CSV 历史脏行回填到 SQLite（只清理测试脏行 + 停止新脏写入；历史归档保留 .bak）。
- 前端展示改造（`caisen.ts` 死端点属既有 spec 待办，不并入）。
- 多账户支持、ETF/期权等新资产类别。

---

## 3. 架构与设计

### 3.1 W1 网关就绪与重连（P0）

#### 3.1.1 就绪信号重定义

`is_client_ready()` 弃用“启动缓存文件 mtime ≤ 5min”判据，改为两级判定：

1. **进程级**：`XtMiniQmt.exe` 存在（或 userdata 目录存在且非空）→ 弱就绪；
2. **活跃级**：`userdata_mini\quoter\<市场>` 目录 mtime 或当天 `XtMiniQuote_*.log` mtime 在 `staleness_sec`（默认 300s）内 → 强就绪。

两者皆无 → `not ready`（打 WARNING，见 3.1.2）。弱就绪但强就绪缺失 → **允许直接尝试 connect 一次**，由返回码定权威结论（0=成功；-1=清残留重试；其他非零=客户端未登录/环境故障），不再用文件活跃度做硬前置。

**为什么这样改**：connect 返回码是客户端可用性的唯一权威信号；文件 mtime 只是启发式。既有 connect -1 自愈（stop-before-recreate + 清理重试）已能区分“session 残留”与“环境故障”，就绪闸只需挡“客户端进程完全不在”这一种必然失败场景。

#### 3.1.2 health_guard 可见性

- ④ 未就绪分支：新增 `logger.warning("health_guard 客户端未就绪（进程=%s，文件最新=%s，陈旧 %d 分钟）")`；连续未就绪每 10 轮推一次钉钉 WARN（复用现有 `_alert_critical` 通道或新增 WARN 通道，节流策略同重连失败 `% 10`）。
- 区分文案：`文件不存在` vs `文件陈旧 N 分钟` vs `进程在但无活跃行情`。
- 重连失败已有点（`health_guard 重连失败`），补齐“从未尝试”的可见性即可。

#### 3.1.3 connect 前清理残留队列

把 `_cleanup_session_files` 从“-1 后补救”前置为“每次 connect 尝试前预防”：

```
connect():
    stop-before-recreate（既有）
    _cleanup_session_files(userdata, sid)   # 新增：防 75MB 旧队列重放
    for attempt in (1, 2):
        ...
```

同时删除“清理后仍失败”路径对 CSV 的影响：旧队列里的成交回报事件被清掉后不会重放；CSV 幂等（W3）作为第二道防线。

#### 3.1.4 单引擎硬约束

- `start_server.bat` 与 schtasks `QuanterServer` 保持唯一入口；`trading/__main__.py` 启动时先探测 port 8000 占用 + 同 session 存活进程，双命中即退出（`sys.exit` 前打 CRITICAL）。
- `single_instance.acquire` 增加“锁持有者 PID 探活”：锁文件正常但持有者进程不存在 → 视为死锁，允许接管（当前 OS 锁随进程退出自动释放，理论上无需；加探活兜底防锁文件残留误判）。
- 运维侧：清理现有 37168/35736 等多余进程，只保留 schtasks 拉起的一条链。

### 3.2 W2 计划生命周期真相源（P0）

#### 3.2.1 veto/confirm 双写

- `veto_plan.veto(date, symbol)`：改 JSON 的同时，对每个被否 `trade_id` 调 `state_store.insert_trade_event(account_id, trade_id, symbol, "VETOED")`（幂等 `UNIQUE(account_id, trade_id, action)`）。
- `trading_plan.confirm_plan(date)`：改 JSON 的同时写 `CONFIRMED`（eod_plan 自动确认路径已有 DB 写入，人工确认路径补上）。
- `eod_plan` 重跑：`save_plan` 前先查 DB `get_latest_action(trade_id) == "VETOED"` → 该标的跳过且不写 `confirmed=True`；整单被否（confirmed=False 且 orders 空）→ 直接跳过重写。
- 计划 JSON 原子写：并入 `2026-08-04-reliability-remediation-design.md` #11（临时文件 + `os.replace`），防止半截 JSON 被读成 None。

**测试红线**：veto 后重跑 eod_plan，断言订单不复活；confirm/veto 后 DB trade_event 与 JSON 一致。

### 3.3 W3 成交流水真相源收敛（P1）

#### 3.3.1 写入端幂等

`_handle_order_update` 的 trade 分支改为：

```
inserted = _state_store.insert_fill(...)        # 既有幂等
if inserted:
    apply_fill_to_position(...)
    insert_trade_event(FILLED)
    record_live_trade(...)                      # 首次成功才写 CSV
    notify_trade_event(...)                     # 首次成功才推钉钉
```

CSV 与钉钉通知与真相源同一判定点，遵守 `data-source-of-truth.md` 第 3 条。

#### 3.3.2 消费端切 DB

- 交易简报：`query_trades` 改读 `state_store.fill`（按日期 + direction + kind=fill），CSV 仅作导出/复盘归档。
- 导出接口：保留 CSV 导出，但数据源改为从 DB 生成（“镜像可从真相源重建”）。
- 复盘服务：`review_service` 改读 DB，`csv_text` 上传模式保留。
- 简报去重（双保险）：`build_trading_brief` 对 `(timestamp, symbol, shares, price)` 去重，并输出“同一成交重放 N 次”计数段（仅在 N>1 时显示）。

#### 3.3.3 持仓三态

`positions` 渲染区分：broker 权威空仓 → `空仓`；有仓 → 明细；取数失败/降级 → `持仓未知（网关未连接）`，与“资产未取到”同语义。

#### 3.3.4 post_close 兜底改口径

`post_close` 第②段聚合从 `aggregate_fills_by_symbol(CSV)` 改为 `state_store` 的 fill 净持仓（同 `UNIQUE(order_id, traded_time)` 幂等口径）；CSV 只读镜像不再参与账本纠偏。

#### 3.3.5 脏数据清理

按 `scripts/migrate_live_trades_csv.py` 的 `TEST_FILL_SYMBOLS` 口径，清理 `600000.SH/300001.SZ/300002.SZ` 的 `成交回报@` 测试行（先 .bak 归档）；若 QMT 账户侧这些测试成交仍存在，需在客户端/账户层清掉，否则 W1 后仍会重放一次。

### 3.4 W4 日权益快照收口（P1）

- `account_daily` 为唯一日权益表：pre_open 写 `start_total_asset/start_cash/start_snap_at`（`state_store.snapshot_start_equity`），post_close 写收盘 + `daily_pnl = close - start`（既有 `snapshot_close_equity` 逻辑即可闭合）。
- `daily_equity` 降级：保留表结构作熔断基线读口，写入端统一走 `account_daily`（或直接删除 `position_book.snapshot_start_equity/get_start_equity`，熔断改读 `account_daily.start_total_asset`）。
- 巡检：每日对比两表同 date 的 start 值，漂移告警。

### 3.5 W5 数据就绪单口（P1）

新增 `trading/state_store.get_ready(date, datasets) -> bool`（或 `trading/calendar` 侧纯函数）：

- 判定 = `data_ready` 全绿 AND `job_ledger.pipeline(done)` AND（可选）parquet mtime 新鲜；
- pre_open gate③、catchup、data 播报统一调用；任一路写失败（data_ready 或 job_ledger）都可在该函数内显式暴露差异并告警。
- 播报端保留 mtime 双口径展示（观测健康度），但“是否放行挂单”只用单口结果。

### 3.6 W6 治理与巡检（P2）

- 修订 `docs/data-source-of-truth.md`：#3 成交流水 SSoT 改 `state_store.fill`（CSV=可重建镜像）；#7 param_iter 标“已退役，experiment.db 优先，JSON 仅只读回退”；新增 #15 日权益 `account_daily`。
- `expired_positions.json` → 迁入 DB（`trade_event(EXPIRED)` 已存在，删除文件写读，pre_open 改从 DB 读待平列表）。
- `_position_attribution` → 落 `state_store.position`（新增归因列或独立表），重启后可从 fill/order 重建。
- symbol 名称统一读 `data_lake/stock_basic.parquet`（沿用既有待办）。
- 播报幂等收敛为 job_ledger 单口（`.last_<bot>_brief` 降级为兼容读口）。
- smoke 工具默认写 `logs/smoke_tmp/`，禁止直写生产 plan 路径。
- 新增 `scripts/audit_ssot.py`：比对 计划JSON↔DB trade_event / daily_equity↔account_daily / CSV↔fill / 引擎进程数，输出漂移报告，可挂每日巡检。

---

## 4. 分阶段实施顺序

### Phase 0（立即，先于下个交易日）

- W1：就绪信号重定义 + health_guard 告警 + connect 前清理队列
- W2：veto/confirm 双写 + eod 重跑防复活
- 配置回正：`TRADE_SHADOW_MIN_DAYS` 恢复 5；确认 `AUTO_CONFIRM_PLAN` 语义
- 运维：清理多余引擎进程；清理 75MB 残留队列；清理 CSV 测试脏行（.bak 保留）

验收：
- `is_client_ready()` 对“进程在 + quoter 活跃”返 True（新探针单测）
- 客户端运行 1 小时后重启引擎，`_health_guard` 1 分钟内连上（e2e 冒烟）
- veto 后重跑 eod_plan，被否订单不复活（回归测试）
- 未就绪状态出现 WARNING 日志（caplog 断言）

### Phase 1（本周）

- W3：CSV 幂等 + 消费端切 DB + 简报去重/三态 + post_close 改 DB 聚合
- W4：account_daily 闭合 + daily_equity 降级
- W5：ready 单口

验收：
- 重放同一 `(order_id, traded_time)` 成交回报 → CSV 仅 1 行、钉钉 1 条（回归测试）
- 简报对重复行去重且显示“重放 N 次”（fixture 测试）
- 断线时简报显示“持仓未知”，非“当前无持仓”（fixture 测试）
- post_close 不再以 CSV 为准（单测 mock CSV 脏行，断言不重写 position）
- `account_daily` 同 date 有 start + close，`daily_pnl` 非空（e2e 测试）

### Phase 2（计划内）

- W6：文档修订 + 冗余镜像迁移 + 巡检脚本

---

## 5. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 新就绪信号误判（quoter 目录盘后不更新） | 盘后/非交易时段放宽强就绪（仅交易时段查活跃；盘后进程在即放行）；connect 返回码兜底 |
| connect 前清队列误删新事件 | 只清 `down_queue_win_{sid}`（引擎自有会话文件），不动 `xtquant/xtmodel` 队列；删除前校验 mtime 陈旧 + 无活动连接 |
| 消费端切 DB 后历史数据缺失 | state_store.fill 与 CSV 双写期内以 DB 为准；历史 CSV 只读归档，不回填 |
| veto 双写中途失败 | 先写 DB（真相源）再写 JSON；DB 失败则 veto 命令报错退出，不产生“看似成功实际只改一半” |
| 进程清理误杀 schtasks 链 | 只清理非 schtasks 入口的多余进程；保留 `QuanterServer` 拉起的那条链 |

回滚策略：W1/W2 均可单独 revert（就绪探针回旧逻辑 + health_guard 告警保留）；W3 消费端切 DB 前保留 CSV 双写一周，DB 异常时一键切回 CSV 读口（开关 env）。

---

## 6. 附录：问题总账

| # | 问题 | 证据（文件/日志） | 建议修复 | 回归测试 |
|---|---|---|---|---|
| 1 | is_client_ready 探针失效 | [qmt.py:337]；`userdata_mini` 缓存文件 mtime 01:01 vs `quoter\SH` 08:45 | 3.1.1 | 新探针单测（quoter 活跃返 True） |
| 2 | health_guard 静默跳过 | [engine.py:2355] ④ 无日志 | 3.1.2 | caplog 断言 WARNING |
| 3 | 客户端活但探针判死 | 08-04 上午实测 | 3.1.1 | e2e：客户端运行 >5min 后引擎重连 |
| 4 | connect 前不清队列 | [qmt.py:469]；`down_queue_win_123459` 75MB | 3.1.3 | 单测：connect 调用前触发清理 |
| 5 | 双引擎进程 | 进程树 37168→35736；netstat :8000→35736 | 3.1.4 | 启动探测单测 |
| 6 | veto 只写 JSON | [veto_plan.py:58]；全仓无 VETOED 写入方 | 3.2.1 | veto→重跑 eod 断言不复活 |
| 7 | live 配置 | `.env` TRADE_SHADOW_MIN_DAYS=1 | 配置回正 | 配置校验脚本 |
| 8 | CSV 无幂等 | [engine.py:3046] | 3.3.1 | 重放 fixture 断言 CSV 1 行 |
| 9 | 消费端读 CSV 不去重 | [broadcast/__main__.py:172] | 3.3.2 | brief fixture 断言“买 1 笔 + 重放 N 次” |
| 10 | post_close 以 CSV 为准 | [engine.py:1606] | 3.3.4 | mock 脏 CSV，断言不重写 position |
| 11 | 持仓未知显示为空 | [brief_trading.py:91] | 3.3.3 | 断线 fixture 断言“持仓未知” |
| 12 | 权益双表断链 | [engine.py:798]/[engine.py:1813]；account_daily.start 恒 NULL | 3.4 | e2e 断言 daily_pnl 非空 |
| 13 | 数据就绪三源 | [pipeline.py:131]/[data_service.py:94] | 3.5 | 单口函数三源组合测试 |
| 14 | 治理清单过期 | `docs/data-source-of-truth.md` #3/#7 | 3.6 | 文档修订（人工审） |
| 15 | 冗余镜像/内存态 | `expired_positions.json`、`_position_attribution` | 3.6 | 迁移后旧读口删除测试 |

---

## 7. 遗留问题与开放决策

1. **QMT 客户端登录自动化**：本 spec 不做，但建议评估客户端“自动登录 + 保活”脚本（属客户端侧，非引擎职责）。
2. **历史 24 行 CSV**：Phase 0 清理测试脏行；`600000.SH` 若在 QMT 账户侧真实存在，需人工确认是否保留持仓（当前 state_store position 为空，简报显示无持仓）。
3. **旧 `down_queue` 重放**：即使清队列，QMT 侧若仍持有未确认成交，首次连接可能重放一次；W3 的 CSV 幂等 + 简报去重是最终防线。
