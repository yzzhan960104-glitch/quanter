# 实盘准入 Gate Checklist（韧性系统收口 · Task 11）

> **文档定位**：本文是「交易执行韧性系统」（分支 `fix/trading-execution-resilience`，HEAD `040b4fba`，T1-T10 全完成、220 passed）切 live 前**人工执行的硬门检查清单**。spec §6 五硬门 + 模拟盘验证 SOP + **用户强化的真实挂单撤单验证** + 切 live 流程 + R1-R7 follow-up，全部固化于此。
>
> **执行人**：研究员（用户）。AI 协助分析日志，不替按。
> **适用范围**：`.venv310` 解释器、miniQMT 客户端、`AUTO_TRADE_MODE=live` + `AUTO_CONFIRM_PLAN=true` 全自动模式。
> **红线**：缺一硬门不切 live。任一硬门红 → 排查 → 修复 → 重跑该门直到绿，再进下一门。

---

## 0. 前置事实（写文档时已核实，执行时复核）

| 项 | 值 | 来源 |
|---|---|---|
| 当前分支 / HEAD | `fix/trading-execution-resilience` / `040b4fba` | `git log` |
| T1-T10 状态 | 全 complete，全套 **220 passed**（实测 `tests/trading/`） | progress-resilience.md + 实跑 |
| .env `AUTO_TRADE_MODE` | `live` | 已是 live（用户预期，[[config-loaddotenv-test-pollution]]） |
| .env `AUTO_CONFIRM_PLAN` | `true` | 同上 |
| .env `QMT_SESSION_ID` | `123458` | 避客户端 session 冲突（同 account sid 互斥） |
| .env `QMT_ACCOUNT_ID` | `10110356` | |
| position_book DB 路径 | `logs/trading_state.db`（WAL） | `trading/position_book.py:_DEFAULT_DB` |
| 当期 plan | `logs/trading_plans/plan_2026-07-30.json`（300483.SZ 2300@20.86 + 688131.SH 600@80.15，rr 6.77/6.07） | 已存在 |
| 茅台既有持仓 | 600519.SH 100 股（gw 真实持仓） | [[qmt-live-smoke-findings]] |
| golden baseline | `tests/_golden/neckline_baseline.json`（stale EXEC_DEFAULTS hash，[[strategy-unify-backtest-live-plan]]） | 待 `--capture` 刷新 |
| 启动 banner 函数 | `trading/__main__.py:log_startup_banner()` | T4 已实现 |
| 口径自检函数 | `trading/engine.py:_sanity_check_date_alignment()` | T5 已实现 |
| 告警函数 | `trading/engine.py:_alert_critical()` → `infra.notifier.fire_and_forget(notify_risk_event(msg,"CRITICAL"))` | T9 已实现 |

> **plan 标的以当期 plan_YYYY-MM-DD.json 为准**。本 checklist 用 `plan_2026-07-30.json`（300483.SZ + 688131.SH）作示例；执行日若已过 07-30，替换为当期最新 plan 的标的逐字段核对。

---

## 1. 五硬门（spec §6 · 缺一不切 live）

### 硬门 ① 全套单测 + e2e 绿（基线 220 passed）

**命令**：
```bash
F:/quanter/.venv310/Scripts/python.exe -m pytest tests/trading/ -v
```

**通过判据**：末行 `===== 220 passed in X.XXs =====`（或更高，不得出现 failed/error）。

**记录**：
- [ ] 实测通过数：____ passed / ____ failed
- [ ] 耗时：____s
- [ ] 执行时间：____-__-__ __:__
- [ ] 与基线（220）差异说明：____________

**红处理**：failed 项优先看是否 `.env` 污染（[[config-loaddotenv-test-pollution]]，conftest 已 autouse 隔离，但新测试若依赖真实 .env 会漏网）；其次看是否 plan 标的/日历相关 mock 漂移。零新增失败才放行。

---

### 硬门 ② 模拟盘 golden 全链路验证（qmt_live_smoke AUTO 模式）

**脚本**：`trading/tools/qmt_live_smoke.py`（AUTO 模式全链路：connect → 挂单 → 撤单确认 → 断线重连自愈）。

**运行**：
```bash
F:/quanter/.venv310/Scripts/python.exe trading/tools/qmt_live_smoke.py
```
（脚本内置 step gate，步骤 10 真单+撤单需交互确认；详见脚本 docstring）

**通过判据**（全链路 4 断点逐项 ✓）：
- [ ] **步骤 1 connect**：`_connected=True`、`_lock_down=False`、`_main_push_available=True`
- [ ] **步骤 3 positions**：读到茅台 600519.SH 100 股（验证读 gw 真实持仓正确）
- [ ] **挂单**：真实最小限价单挂出 → miniQMT 客户端委托列表出现该单
- [ ] **撤单确认**：`cancel_order` 后 `_confirm_cancelled` 轮询到 `CANCELLED` 终态 → 客户端单子状态变 CANCELLED → 返 True
- [ ] **断线重连自愈**：断线后 `health_guard` job 探测 `is_client_ready=True` → 自动 `connect()` 恢复 `_connected=True`（见 T8 实现）

**记录**：
- [ ] 全链路截图 / 客户端委托记录：____
- [ ] 推送回报观察（撤单主推延迟实测 1-2s，[[qmt-live-smoke-findings]]）：____s
- [ ] 异常（若有）：____________

**红处理**：connect -1 → 跑 `scripts/qmt_clear_session_lock.py`（硬门 ② 的工具支撑，见 SOP §2 前置）；挂单被拒（模拟盘拒涨停价买单 → 改挂卖一价）；撤单主推延迟超 5s → `_confirm_cancelled` 超时返 False 计 unconfirmed。

---

### 硬门 ③ golden baseline 刷新（解锁 --verify）

**背景**：[[strategy-unify-backtest-live-plan]] 收尾时 `EXEC_DEFAULTS` stale hash 致 `regression_neckline_golden.py --verify` 阻塞，须 `--capture` 刷新锚。

**命令**：
```bash
# 先刷新（固定 3 标的 + DEFAULTS + EXEC_DEFAULTS 黑盒数值重算落盘）
F:/quanter/.venv310/Scripts/python.exe backtest/tools/regression_neckline_golden.py --capture

# 再验证（== 一致，纯重构零退化铁证）
F:/quanter/.venv310/Scripts/python.exe backtest/tools/regression_neckline_golden.py --verify
```

**通过判据**：
- [ ] `--capture` 生成 `tests/_golden/neckline_baseline.json`（含 DEFAULTS/EXEC_DEFAULTS sha256 指纹）
- [ ] `--verify` 退出码 0，输出「baseline 一致」（逐位 ==，1e-9 容差内）
- [ ] EXEC_DEFAULTS hash 与当前 `strategies/neckline/backtest.py` 默认值匹配（不再 stale）

**记录**：
- [ ] DEFAULTS hash：____
- [ ] EXEC_DEFAULTS hash：____
- [ ] verify 结果：____________

**红处理**：verify 报「参数指纹变化」= EXEC_DEFAULTS 真改了（预期，重 capture）；报「数值漂移」= 非纯重构引入 bug，回查 strategies 改动（[[strategy-unify-backtest-live-plan]] Task5 golden 零退化方法论）。

---

### 硬门 ④ M4 钉钉告警模拟盘实测收到 CRITICAL

**背景**：T9 接入了 3 个致命事件点（pre_open 漏挂 submitted=0 / 口径自检失败 / health_guard 重连耗尽 %10）→ `_alert_critical` → `fire_and_forget(notify_risk_event(msg, "CRITICAL"))`。须在模拟盘**实测钉钉群收到** CRITICAL 推送。

**前置（[[broadcast-robot-manager-status]] 待执行项，AI 不替按）**：
- [ ] `.env` 填真值：`DINGTALK_WEBHOOK`（trading 风控告警通道）+ `DINGTALK_SECRET`（加签）已配
- [ ] `.env` 填真值：`CLI_BOT_UNIFIED_APP_ID` / `REVIEW_BOT_UNIFIED_APP_ID` / `BROADCAST_AGENT_WORKDIR`（cli/review connect 类机器人）
- [ ] 钉钉群移除 market 机器人（已下线）+ 删 `DINGTALK_CHAT_ROBOT_CODE`
- [ ] 首次拉起 connect：`python -m broadcast connect --start all`（逐个验，缺 unified-app-id 会 RuntimeError 跳过）

**触发方式**（模拟盘构造致命事件）：
1. **最直接**：网关 lock_down（断开客户端）时跑一轮 pre_open → submitted=0 → 触发「pre_open 漏挂 submitted=0」CRITICAL
2. 备选：让 `_sanity_check_date_alignment` 返 False（mock next_trading_day=today）→ 触发「口径自检失败」CRITICAL

**通过判据**：
- [ ] 钉钉 trading 风控群收到 CRITICAL 推送（⚠️/❌/🚨 前缀，语义=风险需介入）
- [ ] 消息内容含致命事件关键字（「漏挂」/「口径自检失败」/「重连耗尽」之一）
- [ ] 截图 / 消息 id 留档：____________

**红处理**：钉钉没收到 → 先查 `DINGTALK_WEBHOOK`/`SECRET` 是否真值；再查 `fire_and_forget` 是否吞异常（`infra/notifier.py:233`，看日志有无「fire_and_forget 后台协程失败」）；告警风暴保护：CRITICAL 仅限致命事件 + 去重计数（spec §7）。

---

### 硬门 ⑤ 启动 banner + 口径自检在模拟盘日志绿

**触发**：`python -m trading`（或 `start_all` 重启 engine）启动时。

**通过判据**（日志两行必须出现且语义正确）：
- [ ] **启动 banner**（`trading/__main__.py:log_startup_banner`）：
  ```
  === 启动 banner === session=123458 account=10110356 userdata=<QMT_USERDATA_PATH> mode=live confirm=true | 口径: eod=next_trading_day, pre_open=today（标的 T+1 对齐）
  ```
  - session=**123458**（非 123456——[[qmt-connect-1-rootcause]] 故障即进程内 session 漂移到 123456 无 banner 发现）
  - mode=**live**、confirm=**true**
- [ ] **口径自检通过**（`trading/engine.py:_sanity_check_date_alignment`）：
  ```
  口径自检通过：eod 落盘 key=<T+1 日期>，pre_open 次日读 today 与之对齐
  ```
  - key 必须是 **next_trading_day(today)**（次日），不得等于 today（旧 bug 口径会返 today → 次日读 T+1 永远差一天永不挂单，[[eod-date-offbyone-fix]]）

**记录**：
- [ ] banner 原始日志行：____________
- [ ] 口径自检原始日志行：____________
- [ ] session 漂移检查（进程内 vs .env 一致）：✓

**红处理**：banner 显示 session=123456 → 进程读了旧 env / 未重启 / sys.path 遮蔽（[[syspath-calendar-shadowing]]）；口径自检失败（「未算出次日」）→ 跑了旧代码，**拒绝进 live**，重启 engine 加载新代码（T9 已接 CRITICAL 告警，硬门 ④ 应同步收到）。

---

## 2. 真实挂单撤单验证 SOP（用户强化 · 核心）

> **用户强化要求**：所有任务完成后**真实挂单 + 撤单验证**，须保证 **qmt 客户端显示与计划对齐**（不只模拟盘 dry_run）。本 SOP 是硬门 ② 的**实操细化**，逐字段核客户端 vs plan JSON，并在 position_book 账本同步层面二次校验。

### 2.1 前置（执行前全部 ✓）

- [ ] **miniQMT 客户端已登录**：打开客户端 → 登录账号 `10110356` → 确认 userdata 路径与 `.env QMT_USERDATA_PATH` 一致
- [ ] **.env = live**：`AUTO_TRADE_MODE=live`、`AUTO_CONFIRM_PLAN=true`、`QMT_SESSION_ID=123458`、`QMT_ACCOUNT_ID=10110356`（核对，不改）
- [ ] **kill 旧 engine 进程树**：避免旧进程占 sid（connect -1）或跑旧代码（口径漂移）
  ```bash
  # Windows 找 python trading 进程
  tasklist | findstr python
  # 按 PID 树杀（替换 <PID>）
  taskkill /F /T /PID <PID>
  ```
- [ ] **清 session 锁残留**（M5 工具，交互式防误删）：
  ```bash
  F:/quanter/.venv310/Scripts/python.exe scripts/qmt_clear_session_lock.py
  ```
  - 确认「[保护·不动]」含当前 sid=123458 的文件（绝不清）
  - 仅对「[可清·残留]」（非当前 sid 且 mtime>1h）输入 `yes` 删除
- [ ] **start_all 重启 engine**：
  ```bash
  # 用户侧 start_all 入口（schtasks 或直跑，按现场）
  python -m trading
  ```
- [ ] **看 banner**（硬门 ⑤）：日志首屏确认 `session=123458 mode=live` + `口径自检通过`，否则停下排查

### 2.2 真实挂单（核 qmt 客户端委托 vs plan JSON 逐字段）

**触发方式**：等 `pre_open` 到点自动挂（全自动模式），或手动触发一次 pre_open。

**以 `plan_2026-07-30.json` 为例**（当期 plan 替换为最新）：

| 字段 | plan JSON 值 | qmt 客户端委托显示 | 核对 ✓ |
|---|---|---|---|
| 标的 1 | `300483.SZ` | 300483.SZ | [ ] |
| qty 1 | `2300` | 2300 | [ ] |
| price 1 | `20.86` | 20.86 | [ ] |
| 方向 1 | `buy`（买单） | 买入 | [ ] |
| 标的 2 | `688131.SH` | 688131.SH | [ ] |
| qty 2 | `600` | 600 | [ ] |
| price 2 | `80.15` | 80.15 | [ ] |
| 方向 2 | `buy`（买单） | 买入 | [ ] |

**通过判据**：
- [ ] 两笔委托**均出现在 miniQMT 客户端委托列表**
- [ ] 标的 / qty / price / 方向**四字段逐项一致**（price 模拟盘拒涨停价买单时改挂卖一价，须记录实际挂价与原因）
- [ ] 委托状态 = 报送中 / 已报（非废单）

**记录**：
- [ ] 实际挂价（若有调整）：300483.SZ=__、688131.SH=__
- [ ] 客户端委托截图：____
- [ ] seq ↔ real_oid 双 ID 对账（[[qmt-live-smoke-findings]] `_seq_to_real`）：plan seq → 客户端委托号映射留档

**红处理**：标的不一致 → pre_open 读错 plan（日期口径，硬门 ⑤）；qty/price 不一致 → plan JSON 与提交逻辑字段映射错；全部废单 → 网关 lock_down 或价格违规，查 `_alert_critical` 是否触发漏挂告警。

### 2.3 真实撤单（核 CANCELLED + _confirm_cancelled + position_book）

**触发方式**（二选一）：
- **A. cancel_on 触发**：盘中价格摸到 `cancel_on`（300483.SZ 23.86 / 688131.SH 88.97）→ `decide_exit` pending CANCEL_ON 自动撤单（[[strategy-unify-backtest-live-plan]]，执行层用 high 盘中摸高）
- **B. 次日 pre_open 撤昨日单**：`trading/io/breaker.py:cancel_all_open_orders` 撤所有可撤单

**通过判据**（三层确认）：
- [ ] **客户端层**：miniQMT 客户端单子状态变 **CANCELLED**（已撤）
- [ ] **网关层**：`_confirm_cancelled(oid)` 轮询 query_orders 到 `CANCELLED` 终态 → 返 **True**（撤单主推延迟 1-2s 内轮询到）
- [ ] **账本层**：`position_book`（`logs/trading_state.db`）同步——撤单的标的若无成交则不记 fill；若有部分成交则 apply_fill 增量记账（R-1 部分成交精度，[[trading-gap4-position-book-status]]）

**记录**：
- [ ] 撤单触发方式：cancel_on / pre_open
- [ ] 客户端 CANCELLED 截图：____
- [ ] `_confirm_cancelled` 返回：True / False（False 计 unconfirmed → 查 breaker 返回 `unconfirmed` 计数）
- [ ] position_book 查验（sqlite3 读 logs/trading_state.db fill/position 表）：____________

**红处理**：客户端显示 CANCELLED 但 `_confirm_cancelled` 返 False → query_orders 降级返 []（lock_down），主推延迟超 5s timeout；unconfirmed>0 → 查日志「撤单未确认终态 order_id=」，人工复核柜台。

### 2.4 持仓对账（茅台 + 新挂，gw 真实持仓 vs position_book）

**目的**：验证 `stop_loss_monitor` 读 gw 真实持仓正确（spec §8.12 + [[qmt-live-smoke-findings]] 茅台串通挂撤成交实测）。

**对账两标的**：
- [ ] **茅台 600519.SH 100 股**（既有持仓，gw 权威）：
  - gw `query_positions` 读到 600519.SH qty=100 ✓
  - position_book `logs/trading_state.db` position 表 600519.SH 记录与 gw 一致（或既有的对账差异说明）
- [ ] **新挂标的**（300483.SZ / 688131.SH 若成交）：
  - gw `query_positions` 读到实际成交持仓
  - position_book apply_fill 记账一致（部分成交增量幂等，R-1）

**通过判据**：
- [ ] gw 真实持仓 = position_book 账本（差异须有明确原因：未成交撤单 / 手续费 / 延迟）
- [ ] `stop_loss_monitor` 据 gw 持仓正确触发 stop_loss / take_profit（decide_exit 单源，[[strategy-unify-backtest-live-plan]]）

**记录**：
- [ ] gw 持仓快照：____________
- [ ] position_book 快照：____________
- [ ] 差异说明（若有）：____________

**红处理**：gw 持仓 vs position_book 漂移 → apply_fill 漏记（db lock/异常软降级）→ 盘后 query_trades 兜底纠正（R-1 双保险，[[trading-gap4-position-book-status]] gap4 是唯一真 gap，live 前必修 4 项之一）。

---

## 3. 切 live 流程

**前提**：硬门 ①②③④⑤ **全绿** + §2 真实挂单撤单验证全绿 + 研究员（用户）签字。

**切 live 步骤**：

- [ ] **研究员签字**：5 硬门 + SOP 全绿确认，签字（日期 + 决策）：____________
- [ ] **.env 确认 live**：`AUTO_TRADE_MODE=live`（已是 live，核对无需改）
- [ ] **重启 engine 生效**：
  ```bash
  # kill 旧进程树（SOP §2.1）+ start_all 重启
  python -m trading
  ```
- [ ] **日志验证**：首屏出现
  ```
  === 启动 banner === session=123458 ... mode=live ...
  口径自检通过：eod 落盘 key=<T+1>，pre_open 次日读 today 与之对齐
  网关已连接 session=123458
  ```
  （`网关已连接 session=123458` = connect 成功、_connected=True、_lock_down=False）
- [ ] **进入 live**：cron 到点（pre_open / stop_loss_monitor / post_close）自动执行，实盘真金

**切 live 后首日观察**（spec §8.12 硬要求，1-2 交易日）：
- [ ] cancel_on 撤单触发正确（decide_exit pending CANCEL_ON 用 high 盘中摸高）
- [ ] tp1 分级成交（_place_take_profit tp1/tp2 两腿预挂限价单）
- [ ] decide_exit 止损触发（STOP_LOSS 发市价单）
- [ ] bar 构造（xtdata 当日累积 high/low）真实行情准确

---

## 4. R1-R7 follow-up（1 月推演风险，切 live 后排期）

> **范围**：本韧性系统（T1-T11）外的已知风险，来自 [[strategy-unify-backtest-live-plan]] follow-up + [[qmt-live-smoke-findings]] + live-readiness spec R-1/R-2/R-3 + [[neckline-algorithm-gaps]] + [[data-lake-integrity-gap]]。切 live 后按严重度排期，**不阻塞本次切 live**（硬门已守住已知风险），但须登记进 sprint。

| 编号 | 风险 | 来源 | 影响 | 缓解 / 排期 |
|---|---|---|---|---|
| **R1** | pre_open 补挂——网关断线/锁死期间 pre_open 漏挂，重连后无补挂机制 | 1 月推演 | 漏挂标的错过入场 | live 后排期：health_guard 重连成功后触发补挂 pre_open（submitted<plan orders 时重试） |
| **R2** | xtdata fallback——stop_loss 现价依赖 xtdata 当日累积 high/low，EMT 网关无该行情源 | live-readiness spec §8、[[strategy-unify-backtest-live-plan]] R6 | stop_loss 取价失败盘中裸奔 | 另立项（EMT 行情源 stop_loss 兼容） |
| **R3** | 熔断基线兜底——日内熔断 daily -3%（R-2）依赖 daily_equity 表 start_equity 基线，缺失则跳过+告警 | live-readiness spec §5.2 | 极端行情无熔断保护 | live 后排期：daily_equity 基线初始化 + post_close 三步串联（pre_open 快照 → post_close 判定 → emergency_halt） |
| **R4** | lake 完整性——停牌复牌段漏采致识别失真（[[data-lake-integrity-gap]] 300214.SZ 缺 07-14~07-21 已验证） | data-lake | 标的识别假信号 / 漏标的 | 已有完整性 gate（[[data-lake-integrity-gap]] sync 增量不补 d0 前缺口）；排期：suspend_d 停牌识别 + 补采脚本 + 完整性校验强化 |
| **R5** | 部分成交精度——position_book apply_fill 漏记（db lock/异常）致账本漂移 | live-readiness spec R-1、[[trading-gap4-position-book-status]] gap4（live 前必修 4 项之一） | 持仓账本不准 → stop_loss/止盈决策错 | **live 前必修**（gap4）：盘中增量幂等 + 盘后 query_trades 兜底纠正 |
| **R6** | 极端行情——突发地缘/流动性枯竭（2022 大宗商品级）滑点失控、重复废单、保证金不足 | CLAUDE.md 拷问三连 | 滑点失控 / 逼空 / Margin Call | live 后排期：滑点上限保护 + 重复发单去重 + 保证金预检（broker query_asset） |
| **R7** | cancel_on / bar 延迟——decide_exit pending 用 high 盘中摸高，xtdata bar 延迟或 cancel 主推延迟致止盈/撤单不及时 | [[strategy-unify-backtest-live-plan]] follow-up 1、[[qmt-live-smoke-findings]] | 止盈漏触发（`_tp_placed` 未持久化 + monitor skip 兜底缺失） | live 后排期：`_tp_placed` 持久化 + 未成交巡检兜底（Task9 I-1 follow-up） |

**其他 Minor follow-up**（不阻塞，登记）：
- spec §8.4 措辞澄清（close 限识别层；执行层 pending high）
- Task3 scan_symbol `sim["suppression"]=None` → detect_signal 补 Signal.suppression optional
- Task5 非冠军 trailing 开档 golden 补齐（当前 golden 只覆盖默认档 trailing 关）
- [[neckline-algorithm-gaps]] 3 缺口（R1 cancel_on 替代 / cancel_on + 1/4 成交可行性 / 两套 stop 口径 rr 守卫脱节）

**收口记录（2026-08-03 · codex/live-followup-hardening）**：
- R1 pre_open 补挂：已做——`health_guard` 重连成功后调 `catchup._catchup_pre_open`（窗口
  [09:22, `ENGINE_PRE_OPEN_CATCHUP_UNTIL`)，ledger 幂等，已 done 跳过、过窗 CRITICAL）。
- R2 xtdata fallback：降级收口——EMT 已废弃不另接行情源；补「行情源整体失效（全标的
  last_price 缺失）→ live CRITICAL 告警（30min 节流）」兜底止损链路裸奔。
- QMT connect -1：新增 session 级单实例锁（`trading/single_instance.py`，live bootstrap
  连网关前持有，第二实例拒连），补齐 SERVER_PORT 覆盖时端口天然单例失效的缺口。
- 仍 backlog（按必要性评估）：R6 极端行情保护（限价单 + DB 幂等已覆盖大部分，可选现金
  预检）、plan→SQLite、scripts/ 清空；C-7 运维前置为实机操作（重启前执行）。

---

## 5. 自检（文档质量）

- [x] **无占位**：全文无 TBD/TODO/此处略（R-排期项是已知风险登记，非占位）
- [x] **内部一致**：5 硬门引用的函数/路径/参数（banner session=123458、口径 next_trading_day、position_book logs/trading_state.db、golden --capture/--verify）与源码一致
- [x] **可执行**：每步骤具体到命令（pytest / qmt_live_smoke / qmt_clear_session_lock / regression_neckline_golden / taskkill / sqlite3）
- [x] **spec 覆盖**：spec §6 五硬门 + §8.12 模拟盘硬要求 + [[strategy-unify-backtest-live-plan]] live 前三必做 + 用户强化真实挂单撤单 + R1-R7 follow-up 全覆盖
- [x] **红线明确**：缺一硬门不切 live；session 漂移/口径坏拒绝进 live；清锁脚本不删活跃队列

---

**文档版本**：2026-07-29 · Task 11 收口 · 分支 `fix/trading-execution-resilience` HEAD `040b4fba`
**关联**：[[strategy-unify-backtest-live-plan]] · [[qmt-live-smoke-findings]] · [[broadcast-robot-manager-status]] · [[trading-gap4-position-book-status]] · [[eod-date-offbyone-fix]] · [[neckline-algorithm-gaps]] · [[data-lake-integrity-gap]]
