# 自动化链路自愈闭环设计（automation-chain-selfheal · A/B/C/D）

- **日期**：2026-08-07
- **分支**：master @ 444c4caf（spec 阶段）
- **状态**：设计已批准（2026-08-07 用户选择方案 1：单 spec 四模块；决策点采用推荐值）
- **目标**：让「网关断连→残留→抢会话→错过挂单窗口」这条事故链不再需要人工重启就能自愈
- **关联**：
  - `docs/superpowers/specs/2026-08-05-process-gateway-ssot-final-design.md`（进程/网关/风控治理总纲）
  - `docs/superpowers/plans/2026-08-06-process-gateway-phase1.md`（A1/A2/B1-B3/C-8 前置已落地）
  - `docs/superpowers/plans/2026-08-04-gateway-ssot-hardening.md`（W1.1-W1.4、C-8 台账语义）
  - `docs/superpowers/runbooks/2026-08-04-gateway-ops.md`（运维 SOP）

---

## 1. 背景与事故证据（2026-08-07）

08-07 09:22 pre_open 未挂单，09:30 靠人工重启引擎 + 清队列后由 C-8 补跑成功（submitted=3/3）。
事故链逐环证据：

| 时间 | 事件 | 证据 |
|---|---|---|
| 08-06 18:17 | 引擎上线（live），网关连接成功 | banner + `网关已连接` |
| 08-06 18:50 | L2 轮换：preferred 123459 → actual 123461 | `logs/engine_session.json` rotated_at |
| 08-07 00:05 | 网关开始 `connect -1`，连续失败 46+ 次（退避 ~7min/次） | health_guard WARNING |
| 08-07 01:05:30 | XtMiniQmt 客户端被重启 | 进程 StartTime |
| 08-07 09:22 | pre_open gate 未通过「网关未连接」→ 台账 skipped | job_run + 日志 |
| 08-07 09:30 | 人工清流氓进程 + 删 8 个残留队列（含 75MB）→ 重启引擎 → C-8 补跑 3/3 | 日志 + DB |

四个根因（对应四个模块）：

1. **A · broker_oid 跨日复用**：今天 300779 的柜台单号 1048577 撞昨天 688160 的行，
   `get_order_by_broker_oid` 把昨天的行从 REJECTED 误改为 SUBMITTED（对账错配）。
2. **B · 运行中不重试 skipped**：C-8 补跑只在启动时执行；运行中的引擎在网关恢复后
   没有任何逻辑重试被 skipped 的 pre_open（今天靠重启才救回）。
3. **C · 队列清理被三处打败**：句柄锁定（WinError 5 只 warning 不升级）、guard 保护
   当前 sid（75MB 永不清理）、无「客户端重启 ⇒ 旧 sid 全作废」感知。
4. **D · dry_run 与 live 未完全隔离**：bootstrap 不分 mode 都连真实网关；session 锁
   只 live 生效；端口可被 SERVER_PORT 绕过——疑似 dry_run 实例（34184）无锁占会话。

---

## 2. 目标与非目标

### 目标

1. 跨日同 broker_oid 不再误改旧订单行（模块 A）。
2. 网关恢复后，窗口内被 skipped/failed 的 pre_open 自动重试，无需重启（模块 B）。
3. 客户端重启 + 引擎断连后，残留队列（含当前 sid、含 75MB 级）≤5 分钟自动清；
   清不掉时 CRITICAL 可见（模块 C）。
4. dry_run 默认不连真实网关；显式连时用独立 sid 区间，与生产互不干扰；非 dev 启动
   检测到生产引擎在跑时拒绝（模块 D）。
5. 全量 pytest + `ops/run_checks.py` 契约门绿。

### 非目标

- 不做「优雅停机优先」的停机重构（模块 D 只做隔离，停机强杀问题另立）。
- 不改 QMT 客户端侧自动登录（人工 SOP 已落 runbook）。
- 不重建 C-8 启动补跑机制（只加运行中触发点）。
- 不回填历史错误订单状态（A 只修未来匹配语义）。

---

## 3. 总体架构（一句话）

**断连可清（C）、恢复可补（B）、隔离不互踩（D）、单号不错配（A）**——四模块独立可回滚，
共用既有事实源（state_store / job_ledger / process_topology / infra.notifier）。

```
客户端重启 ──▶ C2 guard 检测 StartTime 变化 ──▶ 引擎断连 → force 清全部队列
                                                     │
connect -1 ──▶ C1 强制 stop+重试删除 ──▶ 仍失败 → CRITICAL 告警
                                                     │
health_guard 重连成功 ──▶ B 窗口内重试 skipped/failed pre_open（幂等）
                                                     │
pre_open 挂单 ──▶ A broker_oid 按 trade_date 限定查询/推进（防跨日误改）
                                                     │
dry_run 实例 ──▶ D1 默认不连网关 / D2 独立 sid 区间 / D3 生产在跑时拒启
```

---

## 4. 模块 A：broker_oid 跨日隔离

### 现状

- `state_store.get_order_by_broker_oid(broker_oid)` / `update_order_state_by_broker_oid(...)`
  只按 broker_oid 匹配；QMT 柜台 order_id 跨日复用（08-07 实证）。
- 消费点：`engine._advance_order_state_from_status`（状态推进）、`_handle_order_update`
  （async_response 回填）、cancel 路径 `engine.py:1928`。

### 设计

- `state_store` 两个函数增加 `trade_date: str | None = None` 参数：显式传入时
  SQL 加 `AND trade_date=?`；不传时保持旧行为（兼容测试/手动路径）。
- `engine._advance_order_state_from_status`：查询/推进一律传
  `trade_date=clock.today()`（状态推进发生在当日回报，用当日日期天然隔离跨日复用）。
- `_handle_order_update` 的 async_response 回填、cancel 路径同样传当日日期。

### 接口

- `get_order_by_broker_oid(broker_oid: str, trade_date: str | None = None) -> Row | None`
- `update_order_state_by_broker_oid(broker_oid: str, *, trade_date: str | None = None, ...) -> int`

### 测试

- 同 broker_oid 两天两行：推进今日行不影响昨日行。
- 不传 trade_date 的旧调用行为不变（回归）。
- 回填/撤单路径同断言。

---

## 5. 模块 B：运行中 skipped pre_open 自动重试

### 现状

- `catchup._catchup_pre_open` 只在 `run_startup_catchup`（启动时）执行；
  运行中的引擎对 skipped/failed 无重试触发点。

### 设计

- `TradingEngine._health_guard` 重连成功分支（`_connected` 由 False 变 True 且本轮
  connect 成功）末尾调用新增 `_retry_skipped_pre_open()`。
- `_retry_skipped_pre_open` 逻辑：
  1. `calendar.is_trading_day(today)` 且 `clock.now()` ∈
     `[catchup.WINDOW_START, catchup._catchup_until())`；
  2. `job_ledger.latest_status("pre_open", today) in {"skipped", "failed"}`；
  3. `self._pre_open_retry_in_flight` 防并发（进入置 True，finally 复位）；
  4. 调模块级 `pre_open(today)`（与 cron/启动补跑同一入口，`has_order(OPEN)` 幂等）。
- 日志：重试触发/结果按 A2 语义落台账（done/failed/skipped）。

### 接口

- `TradingEngine._retry_skipped_pre_open() -> None`（async，内部 try/except 全兜底）
- 复用 `catchup.WINDOW_START` / `catchup._catchup_until()`（不重复定义窗口）。

### 测试

- 重连成功 + 台账 skipped + 窗口内 → pre_open 被调一次，台账最终 done。
- 台账 done/running → 不重试。
- 窗口外 → 不重试。
- 重试失败 → 台账 failed（A2 语义），不抛、不阻塞 health_guard。

---

## 6. 模块 C：队列自动清理升级

### 现状

- `broker.qmt._cleanup_session_files` 删除失败只 `logger.warning`（WinError 5 静默跳过）。
- `scripts/qmt_clear_session_lock.is_clearable` 对当前 sid 一律保护。
- guard `cleanup_stale_queues` 只清「非当前 sid 且 >1h」。

### 设计

**C1 · connect 清理升级（broker/qmt.py）**
- 新增 `_cleanup_session_files_checked(userdata, sid) -> tuple[list[str], list[str]]`：
  返回 `(cleaned, blocked)`；原 `_cleanup_session_files` 保持旧签名（返回 cleaned）供
  guard/测试复用，内部委托 checked 版。PermissionError 记入 blocked，不抛异常。
- `connect()` 中：`-1` 且 blocked 非空 → `_stop_trader_safely(self._trader)` →
  `await asyncio.sleep(1)` → 重试删除 → 仍 blocked → 失败文案带
  `清理被占用文件: [...]`（health_guard 既有 CRITICAL 告警随之带上详情）。

**C2 · guard 客户端重启检测（ops/miniqmt_guard.py）**
- 新增 `client_start_sso`：`logs/miniqmt_client_start.json` 记录最近一次
  `XtMiniQmt` 进程 StartTime（首次运行记录为基线）。
- `run_once`：读当前 StartTime → 与基线比对：
  - 变化且 `process_topology.port_holder_pid()` 为 None（引擎断连）→
    `cleanup_all_session_queues(force=True)` 清全部 `down_queue_win_*` +
    `lock_*queue_win_*`（断连态无活跃队列误删风险），更新基线 + INFO 日志。
  - 引擎连接中 → 只更新基线（不清理当前 sid）。

**C3 · is_clearable force 参数**
- `is_clearable(lock, current_sid, now, max_age_sec=3600, force=False)`：
  `force=True` 时忽略 current_sid 保护（仅 C2 断连分支调用）。
- guard `cleanup_all_session_queues(force)` 复用 `list_session_locks`。

### 接口

- `broker.qmt._cleanup_session_files(...) -> tuple[list[str], list[str]]`（或新增
  `_cleanup_session_files_checked`；保持旧函数签名兼容测试）
- `ops/miniqmt_guard.client_start_sso()` / `cleanup_all_session_queues(force=False)`
- `scripts/qmt_clear_session_lock.is_clearable(..., force=False)`

### 测试

- connect -1 + 删除被占 → 强制 stop 后重试成功（mock 两次删除）。
- 重试仍失败 → 错误信息含「清理被占用文件」。
- guard：StartTime 变化 + 引擎断连 → force 清全部；引擎连接中 → 不清当前 sid。
- `is_clearable(force=True)` 放行当前 sid；`force=False` 保持保护。

---

## 7. 模块 D：dry_run 与 live 完全隔离

### 现状

- `engine.bootstrap` 不分 mode 都 `gw.connect()`；session 锁只 live；
  端口检查可被 SERVER_PORT 绕过；dev.py 统一入口但未跳过网关连接。

### 设计

**D1 · dry_run 默认不连真实网关**
- `bootstrap`：`_mode()=="dry_run"` 且
  `os.getenv("QUANTER_DEV_CONNECT_GATEWAY") != "1"` → 跳过 `gw.connect()`，并置
  `self._skip_connect = True`（仍构造网关、注册回调、走 DB 初始化）。
- `_health_guard` 尊重 `self._skip_connect`：为 True 时直接 return（dry_run 缺省
  不反复尝试 connect；显式联调 env 打开后 `_skip_connect=False`，恢复既有行为）。
- 显式 `QUANTER_DEV_CONNECT_GATEWAY=1` 时保持现状（开发联调通道）。

**D2 · dev/dry_run 独立 sid 区间**
- `QMT_DEV_SESSION_BASE`（缺省 200000）：当 `QUANTER_DEV_MODE=1` 且
  `QUANTER_DEV_CONNECT_GATEWAY=1` 时，网关 session = base（不再用生产 123459）。
- dev.py `_backend_env` 显式注入 `QMT_DEV_SESSION_BASE`。

**D3 · 生产在跑时非 dev 启动拒绝**
- `_assert_single_instance` 扩展：若 `QUANTER_DEV_MODE != "1"` 且检测到生产引擎
  三合一一致（port 8000 属主 == pid 文件 == 锁持有）→ 直接拒绝（已有端口占用分支
  覆盖 8000；补「不同端口但生产在跑」场景）。

### 接口

- `engine.bootstrap`：新增 `_skip_connect` 判定（helper `_should_connect_gateway()`）
- `broker.qmt.QmtExecutionGateway.__init__`：dev base 解析
- `ops/dev.py._backend_env`：注入 `QMT_DEV_SESSION_BASE`
- `trading/__main__._assert_single_instance`：生产在跑检测

### 测试

- dry_run 缺省：bootstrap 不调 `gw.connect()`；health_guard 不反复 connect。
- `QUANTER_DEV_CONNECT_GATEWAY=1` + dev base：session 用 200000。
- live 模式行为完全不变（回归全量）。
- 生产一致时非 dev dry_run 启动 → SystemExit(1)。

---

## 8. 决策点记录（已批准 2026-08-07）

| # | 决策点 | 批准结论 |
|---|---|---|
| P-A | broker_oid 隔离维度 | 按 trade_date（推荐） |
| P-B | 重试窗口 | 沿用 `ENGINE_PRE_OPEN_CATCHUP_UNTIL`（缺省 10:00），仅 skipped/failed |
| P-C | C2 是否允许清当前 sid | 允许，但仅引擎已确认断连（force） |
| P-D | D1 默认跳过连网关 | 是（推荐）；显式 env 恢复联调 |
| P-D2 | dev 独立 sid 区间 | 本期做（推荐） |
| P-D3 | 非 dev 启动 fail-closed | 本期做（推荐） |

---

## 9. 验收标准

1. 同 broker_oid 两天两行：推进今日行，昨日行状态不变（A 回归测试）。
2. 网关断线恢复后，09:22–10:00 内自动重挂（无需重启），台账 done/failed 语义正确（B）。
3. 客户端重启 + 引擎断连后，全部残留队列（含当前 sid、75MB 级）≤5 分钟清空（C）。
4. 清不掉时 CRITICAL 文案含「清理被占用文件」（C）。
5. dry_run 缺省不连真实网关；显式联调用 200000 区间 sid；生产在跑时非 dev 启动被拒（D）。
6. 全量 pytest + `ops/run_checks.py` 契约门绿。

---

## 10. 风险与回滚

| 风险 | 缓解 |
|---|---|
| B 重试与 cron 并发 | `_pre_open_retry_in_flight` + has_order 幂等 + APScheduler max_instances=1 |
| C force 清当前 sid 误删活跃队列 | 仅引擎断连（端口无属主）才 force；断连态无活跃消费者 |
| D1 跳过 connect 影响开发联调 | 显式 `QUANTER_DEV_CONNECT_GATEWAY=1` 恢复；dev.py 默认不开网关 |
| A 加 trade_date 影响旧调用 | 参数缺省 None 保持旧行为；新调用显式传日期 |

回滚：A/B/C/D 各自独立 commit；B 可单独 revert（health_guard 恢复分支摘除）；C2 可
由 env `QUANTER_GUARD_DISABLE_CLEANUP=1` 关闭。

---

## 11. 测试改造清单（plan 细化时展开）

- `tests/trading/test_state_store.py`：get/update by broker_oid + trade_date 用例。
- `tests/trading/test_engine_order_update_handler.py`：跨日同 oid 不误改。
- `tests/trading/test_engine.py` / `test_catchup.py`：B 重试触发/不触发矩阵。
- `tests/trading/test_qmt_gateway.py`：C1 清理升级（mock 两次删除/仍失败）。
- `tests/ops/test_miniqmt_guard.py`：C2 客户端重启检测 + force 清理。
- `tests/scripts/test_qmt_clear_session_lock.py`：is_clearable force。
- `tests/trading/test_engine_bootstrap.py` / `test_main.py`：D1/D3。
- `tests/ops/test_dev_backend.py`：D2 env 注入。
