# 交易执行韧性系统设计（Trading Execution Resilience）

> 日期：2026-07-29 ｜ 状态：待 review ｜ 作者：AI 研究员 + 用户
> 关联 memory：[[qmt-connect-1-rootcause]]、[[eod-date-offbyone-fix]]、[[qmt-live-smoke-findings]]、[[broadcast-robot-manager-status]]

## 1. 背景与根因（事实，非推测）

2026-07-29 实盘故障：engine 全天拒发单（pre_open `submitted=0/2`），用户被迫手动挂单，且挂的是 07-28 plan 的标的（300654/300779），与 07-29 engine 该挂的 plan（300654/688036）**标的错位**。三个症状收敛为一张根因图：

| 症状 | 根因 | 证据 |
|---|---|---|
| 挂单没生效 | connect 返回 -1 → `_lock_down=True` 永久锁 → `submit_order` 被拒 | 日志 8518-8520；`down_queue_win_123456` mtime 09:15:20 = engine 启动时刻 |
| 撤单没生效（层1） | 同 lock_down——`query_orders` 返[]、`cancel_order` 拒发 | qmt.py:567/619 lock_down 降级 |
| 撤单没生效（层2） | `cancel_order` 调用后直接计数，**不等 CANCELLED 终态**；主推延迟 1-2s 时状态不同步 | engine.py:872-873；[[qmt-live-smoke-findings]] |
| 标的错位 | 代码口径已修（`_eod`传`next_trading_day`/`_pre_open`读`today`），但 **engine 进程未重启加载新代码** + 用户手挂参照旧 plan | engine.py:1527-1528 vs 进程 StartTime 09:15 < .env mtime 09:29 |

**主病根**：网关 connect 一次性失败即永久 lock_down，启动失败路径不走 `_reconnect`（后者只挂 `on_disconnected` 盘中断线），无自愈 → 全天死锁。叠加「致命事件静默（只 WARNING 不告警）」「撤单不确认终态」「进程/配置漂移无防护」四个独立缺口。

## 2. 目标 / 非目标

**目标**
- G1 网关自愈：connect 失败/盘中断线/客户端慢启动 → 后台守护自动重连恢复 live，不再全天死锁。
- G2 撤单确认闭环：每次撤单确认到 CANCELLED 终态（或超时告警），状态不再悬空。
- G3 漂移可见：engine 启动打印 session/account/mode/口径版本 + 口径一致性自检，进程跑旧代码立即可见。
- G4 静默漏单消灭：致命事件（漏挂/重连耗尽/撤单超时/session 占用）推钉钉 CRITICAL。
- G5 完整测试：单测覆盖每模块 + e2e 覆盖四断点场景 + 模拟盘 golden 验证。

**非目标（YAGNI，本次不做）**
- 不做 plan→SQLite 改造（[[plan-sqlite-deferred]]，独立任务）。
- 不重构颈线算法（[[neckline-algorithm-gaps]]，独立线）。
- 不引入新依赖；复用现有 apscheduler / infra.notifier / MockExecutionGateway。
- 不自动启动 miniQMT 客户端（GUI 需手动登录，密码不进 .env）。
- 不自动删 session 锁文件（交互式确认，防误删活跃队列）。

## 3. 总体架构

新增一个「执行韧性层」，横切在 engine 触发点与网关之间，由 5 个独立模块组成。各模块单一职责、可独立测试：

```
miniQMT 客户端（外部 GUI）
        │ 就绪探测（M1）
        ▼
QmtExecutionGateway ── connect 失败/断线 ──► 后台健康守护 job（M1，统一重连入口）
        │                                            │ 成功 → _lock_down=False 恢复 live
        │ pre_open / stop_loss                       │ 耗尽 → 钉钉 CRITICAL（M4）
        ▼
engine 触发点 ── submit_order / cancel_order ──► _confirm_cancelled 确认终态（M2）
        │
        ├── 启动 banner + 口径自检（M3）
        └── 致命事件 → fire_and_forget(notify_risk_event CRITICAL)（M4）

scripts/qmt_connect_diag.py（已有）+ qmt_clear_session_lock.py（M5，交互式清锁）
```

## 4. 模块设计

### M1 · 网关自愈（就绪探测 + 后台守护重连 + 统一入口）— 核心

**现状**：`connect()` 失败直接 `_lock_down=True` + raise；`__main__` 重试 5 次后放弃，cron 照跑但全拒单。`on_disconnected`→`_reconnect` 只覆盖盘中断线。

**改动**

1. `QmtExecutionGateway.is_client_ready() -> bool`（新增，broker/qmt.py）
   - 探测 miniQMT 客户端就绪：`QMT_USERDATA_PATH` 下 `down_queue_win_*` / `miniqmtShm*Cache` 文件 mtime 在近 5 分钟内（活跃 = 客户端在跑）。
   - 纯文件系统检查，不触达 xtquant，零副作用、可在任何环境调用（测试友好）。

2. `_reconnecting: bool` 互斥标志（新增 `__init__`）
   - 统一两条重连路径（on_disconnected 与后台守护）的并发：任一路径重连前置 True、完成置 False；另一路径见 True 即让出。
   - 消除「守护 job 与 on_disconnected 同时重连」的竞态。

3. 后台健康守护 job（新增 `TradingEngine._health_guard`，engine.py；apscheduler interval job，每 60s）
   - 逻辑：`if gw._connected: return`（已连不捣乱）→ `if gw._reconnecting: return`（让出）→ `if not gw.is_client_ready(): return`（客户端没就绪不空跑）→ `await gw.connect()`（成功则 connect 内部清 lock_down 恢复 live，失败则等下轮）。
   - 退避（不改 apscheduler 调度，避免 reschedule 竞态）：守护 job 固定 60s interval，内部用 `_guard_fail_count` 计数——失败越多跳过越多轮次（如 fail_count=1 跳0轮、=2 跳1轮、=3 跳3轮、≥4 跳7轮，等效 60→120→240→480s 封顶 300s 实际由跳过轮数近似），成功清零。每轮只做一次 connect，绝不 sleep 阻塞 apscheduler 执行线程。
   - 注册：`__main__` 启动时与 `_stoploss` 同机制 add_job（无论首次 connect 成败都注册守护）。

4. `connect()` 失败语义调整（broker/qmt.py:348-354）
   - **状态机明确**（消除歧义）：connect 失败仍 `_lock_down=True`（保留拒单防脏读语义，submit_order/query_orders 继续降级拒发），**不新增"未连接态"第三态**。恢复路径靠守护 job 直接再调 `connect()`——connect() 内部不读 `_lock_down`（它只检查 loop/trader/account），故 lock_down 不阻断重连；重连成功后 connect() 末尾 `_lock_down=False, _connected=True`（qmt.py:369-370）自然解锁。
   - 失败仍 raise ConnectionError（保留契约），日志按返回码/异常类型区分「-1 = session 占用疑似」vs「超时」vs「客户端未就绪」，便于 M4 精准告警。
   - **不删除** on_disconnected→_reconnect 盘中自愈路径，仅在 _reconnect 入口加 `_reconnecting` 互斥。

**边界（Grill Me）**
- 守护 job 与 on_disconnected 重连竞态 → `_reconnecting` 互斥 + `is_client_ready` 前置（客户端没就绪时两条路径都不空跑 connect，避免刷柜台）。
- session 占用：connect -1 时日志/告警提示「sid X 疑似被占用，建议跑 qmt_clear_session_lock.py 或换 sid」，**不自动删锁**。

### M2 · 撤单确认闭环

**现状**：`gw.cancel_order(oid)` 后直接计数，不确认终态。

**改动**

1. `QmtExecutionGateway._confirm_cancelled(oid, timeout=5.0, interval=0.5) -> bool`（新增，broker/qmt.py）
   - 轮询 `query_orders()` 直到该 oid 的 state ∈ {CANCELLED, FILLED（撤单前已成交）} 终态，或超时。
   - 返 True=已确认终态；False=超时未确认（调用方告警，绝不假装成功）。
   - lock_down / 未连接时直接返 False（query_orders 已降级返[]）。

2. 接入点
   - `pre_open._cancel_all_open_orders`（engine.py:525 调用处）：撤每单后 `await gw._confirm_cancelled(oid)`，未确认则记 WARNING + 统计 `n_cancel_unconfirmed`。
   - `stop_loss_monitor` pending cancel_on（engine.py:872）：同上。

**边界**
- 撤单低频（pre_open 每日1次 + 少量 pending），0.5s 间隔轮询撞柜台限频风险可接受。
- 终态含 FILLED：撤单时若已成交，撤单失败但状态明确（不重复撤）。

### M3 · 标的口径 + 配置漂移防护

**现状**：代码口径已修（`_eod`传`next_trading_day`、`_pre_open`读`today`），但进程跑旧代码时不可见。

**改动**

1. engine 启动 banner（`__main__` 启动日志增强）
   - 打印 `session_id / account / userdata_path / AUTO_TRADE_MODE / AUTO_CONFIRM_PLAN / 口径版本（eod=next_trading_day, pre_open=today）`。
   - 一眼看出进程内值 vs .env 是否漂移、是否跑新口径代码。

2. 启动口径自检（新增 `TradingEngine._sanity_check_date_alignment`，`__main__` 启动时跑一次）
   - 断言：`calendar.next_trading_day(today)` 落盘 key 与 `_pre_open` 读取 key（today）在 T+1 语义下一致。
   - 失败 → CRITICAL 告警 + 拒绝进入 live（只 dry_run），防标的错位复发。

**边界**：代码口径已对齐，本模块是「防进程漂移」的可见性 + 启动 gate，不改正常路径逻辑。

### M4 · 可观测（钉钉 CRITICAL）

**现状**：致命事件只写 WARNING 日志，钉钉不推（用户事后才发现漏单）。

**改动**：复用 `infra.notifier.NotificationManager.notify_risk_event(msg, "CRITICAL")` + `fire_and_forget`（`_reconnect` 已在用），在以下事件点接入：
- pre_open `submitted=0` 且 mode=live（漏挂）
- 网关重连耗尽（_reconnect 末尾已有 ERROR，确认走钉钉）
- 撤单确认超时（M2 返 False）
- connect -1 且 is_client_ready=True（session 占用疑似）
- 口径自检失败（M3）

**前置依赖**：[[broadcast-robot-manager-status]] 记的 cli/review 机器人 .env 真值需先填（用户侧待执行项）。

### M5 · session 清锁辅助脚本

**现状**：无工具，残留锁文件靠手动删 userdata。

**改动**：`scripts/qmt_clear_session_lock.py`（新增）
- 列出 `QMT_USERDATA_PATH` 下所有 `down_queue_win_*` / `lock_*` / `*_mutex` 文件 + mtime + 关联 sid。
- 交互式选择清理：只允许清「非当前 .env sid 且 mtime 老（>1h）」的残留；当前 sid / 近期活跃的一律拒绝删除（防误删活跃队列）。
- 与已有的 `scripts/qmt_connect_diag.py`（connect -1 根因诊断）配套。

## 5. 测试策略

### 单元测试（每模块独立，MockExecutionGateway + monkeypatch xtquant）
- `tests/trading/test_qmt_health_guard.py`（新）：is_client_ready 文件 mtime 判定 / _reconnecting 互斥 / 守护 job 在 _connected 时 no-op / 守护 job 在客户端未就绪时跳过 / 守护 job 成功 connect 后清 lock_down。
- `tests/trading/test_qmt_cancel_confirm.py`（新）：_confirm_cancelled 轮询到 CANCELLED 返 True / 超时返 False / lock_down 返 False / 终态含 FILLED。
- `tests/trading/test_engine_sanity_check.py`（新）：口径自检通过 / 漂移时拒绝 live。
- 扩 `tests/trading/test_qmt_gateway.py`：connect -1 时日志区分 session 占用 vs 超时。

### e2e（扩 `tests/trading/test_e2e_trading_flow.py`，四断点场景）
1. 网关 lock_down 时 pre_open submitted=0 + 触发钉钉 CRITICAL（断言 fire_and_forget 被调）。
2. 网关恢复（守护 job 重连成功）后下一轮 pre_open 正常挂单。
3. 撤单确认闭环：cancel_order 后 _confirm_cancelled 到 CANCELLED 才计成功。
4. 标的口径自检：next_trading_day 与 today 对齐，load_plan 拿到正确标的。

### 模拟盘（spec §8.12 硬要求，live 前必过）
- 用 `trading/tools/qmt_live_smoke.py`（AUTO 模式）在模拟盘验证全链路：connect → 挂单 → 撤单（含确认）→ 断线重连自愈。
- golden baseline 对齐（[[strategy-unify-backtest-live-plan]] 的 golden 刷新）。

## 6. live 切换 gate（硬门，缺一不切）

1. 全部新增/扩展单测 + e2e 通过（`F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/`）。
2. 模拟盘全链路 golden 验证通过。
3. M4 钉钉告警在模拟盘实测收到 CRITICAL 推送。
4. 研究员（用户）签字。
5. 切 live 前 .env `AUTO_TRADE_MODE` 保持，但 engine 启动时 banner + 口径自检必须绿。

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 守护 job 与 on_disconnected 重连竞态 | `_reconnecting` 互斥 + is_client_ready 前置（客户端没就绪两条路径都不空跑） |
| 清锁脚本误删活跃 session 队列 | 交互式 + 只清非当前 sid 且 mtime>1h 的残留 |
| 撤单轮询撞柜台限频 | 撤单低频 + 0.5s 间隔 + 5s 超时兜底 |
| 守护 job 永久刷日志（客户端一直没开） | is_client_ready 前置 + 指数退避到 300s 上限 |
| 钉钉告警风暴 | CRITICAL 级别仅限致命事件（漏挂/重连耗尽/超时/session占用/口径失败），去重计数 |
| live 改动引入新 bug | 全套测试 + 模拟盘 golden + 启动口径 gate，硬门不达标不切 |

## 8. 实施阶段（给 writing-plans 的骨架）

按依赖顺序，每阶段独立可测、可提交：
1. **M5 清锁脚本 + 诊断脚本配套**（无依赖，先落地工具，立即可用）。
2. **M2 撤单确认闭环**（独立于网关，纯加方法 + 接入）。
3. **M3 banner + 口径自检**（独立，启动可见性）。
4. **M1 网关自愈**（核心，改动 connect 语义 + 守护 job + 统一入口，风险最高放中段充分测）。
5. **M4 钉钉告警接入**（依赖 M1-M3 的事件点确定后接线）。
6. **e2e 四场景 + 模拟盘 golden**（收口验证）。
7. live gate 检查 + 切 live（用户签字）。
