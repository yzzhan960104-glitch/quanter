# 进程·网关·miniQMT 一体化治理最终设计（process-gateway-ssot-final）

- **日期**：2026-08-05（2026-08-06 基于 master @ 033e4a85 复核更新）
- **分支**：master @ 033e4a85（复核基线；spec 本身未入库，待 commit）
- **状态**：定稿 + 复核更新（2026-08-06；含风控废弃删除决策并入）
- **定位**：总纲——Phase A = 风控废弃删除（打通挂单），Phase B = 进程·网关·miniQMT 一体化治理
  （打通链路稳定），最终目的都是打通自动化链路
- **关联**：
  - `docs/superpowers/plans/2026-08-05-risk-controls-removal-pending.md`（Phase A 决策留痕，已并入本 spec §2）
  - `docs/superpowers/specs/2026-08-05-risk-controls-removal-design.md`（Phase A 实施蓝图 DRAFT）
  - `docs/superpowers/plans/2026-08-04-gateway-ssot-hardening.md`（W1.1–W1.4 已落地：connect 返回码权威、health_guard 告警、前置清队列、端口探测）
  - `docs/superpowers/runbooks/2026-08-04-gateway-ops.md`（08-04 事故 SOP：树杀双进程、.env 回正、启动顺序）
  - `docs/superpowers/specs/2026-08-05-ssot-final-hardening-design.md`（Phase A/B/C：CSV 清除、镜像收敛、计划入 DB；**已实施 commit 17a66588..033e4a85**）
  - `docs/superpowers/specs/2026-07-31-c5-process-model-and-gate-design.md`（C-5 进程模型：python -m trading → uvicorn 单入口）

## 0. 实施状态跟踪（2026-08-06 复核 · master @ 033e4a85）

| 项 | 代码现状 | 状态 |
|---|---|---|
| SSoT A/B/C（CSV 删除、镜像收敛、计划入 DB） | 已提交 17a66588..033e4a85 | ✅ 完成 |
| `scripts/audit_ssot.py` 引擎数检查 | 已含 `check_engine_process_count` | ✅ 部分（缺客户端进程/端口属主，见 B3） |
| Phase A：session 关 09:30 误拦 | `trading_service._in_a_share_session()` 未改（仍 09:30 起步） | ❌ 未落地 |
| Phase A：台账 partial/failed + C-8 重试 | `engine.py:697` 仍 done 掩盖 0 成交；`catchup.py:138` 跳过 done | ❌ 未落地 |
| Phase A：版本/重启告警（P0-3） | `__main__.py` 无 git 版本/启动时间打印 | ❌ 未落地 |
| Phase A：schtasks ONSTART + RestartOnFailure（P1-1） | 实测 LogonTrigger，无 RestartOnFailure | ❌ 未落地 |
| Phase A：生产 fail-closed + dry_run 隔离（P1-2） | 无 QUANTER_TESTING / server_lifecycle；dev.py 未解耦 | ❌ 未落地 |
| Phase B1：supervisor / restart / 测试隔离 / dev 统一入口 | `ops/trading_supervisor.py`、`ops/restart_trading.py` 不存在 | ❌ 未落地 |
| Phase B2：guard / ops 端点 / L2 sid 轮换 | `ops/miniqmt_guard.py`、`/api/v1/ops/processes`、`logs/engine_session.json` 不存在 | ❌ 未落地 |

---

## 1. 为什么这三类问题永远在反复

08-04 事故（双引擎嵌套进程 37168→35736 抢 QMT session → connect -1 → 全天锁死）后，
代码侧已经修了 W1.1–W1.4：connect 返回码成为客户端可用性唯一权威、health_guard 未就绪
告警、connect 前置清理 down_queue、端口占用探测。但用户仍然会反复看到：

1. **新/旧进程并存**：多条启动链（schtasks QuanterServer、手动 `python -m trading`、
   系统 Python 直接跑、uvicorn reloader 子进程、dev.py）都能拉起引擎；端口探测只做
   「占用即 exit」，从不问「谁持有、是否合法、pid 文件是否一致」。
2. **网关 live/disconnected 抖动**：status 只回答「连没连上」，不回答「客户端是不是真登录、
   进程是不是还在、队列有没有残留」；重连成功/失败没有客户端诊断的一屏视图。
3. **miniQMT 进程问题**：客户端进程在但没登录/文件陈旧时引擎只能 WARN，没人负责拉起或
   判定「在但假活」；session 队列残留只在 connect 前清一次，无兜底。

**根因是三张脸、一个根**：没有单一进程所有者、没有强制启动顺序、没有客户端生命周期管理。
每次修复只打某一层（端口探测、锁文件、就绪探针、队列清理），「谁启动、谁重启、谁负责客户端、
测试和生产怎么隔离」始终没有收口——所以每个新改动都会再繁殖出旧进程/新进程、live↔disconnected 抖动。

### 1.1 2026-08-05 现场证据（写 spec 时实测）

| 项 | 实测值 | 判定 |
|---|---|---|
| 8000 端口属主 | PID 27592，exe = `C:\Users\yzzhan\AppData\Local\Programs\Python\Python310\python.exe` | ❌ 系统 Python 持有生产端口 |
| schtasks QuanterServer | Running，Task To Run = `F:\quanter\scripts\start_server.bat` | ✅ 入口已指向唯一 bat |
| 同刻 python 进程 | 27592（系统 Python）、70848（`.venv310\Scripts\python.exe`）、79572（系统 Python） | ❌ 三条链并存 |
| miniQMT 客户端 | XtMiniQmt PID 26328 在 | ✅ 进程在（是否登录未探） |

结论：`start_server.bat` 的 venv 绝对路径写对了，但 8000 仍被系统 Python 旧链持有，
且 schtasks 链（70848）大概率因端口被占而处于异常/僵持态。这正是「唯一入口形同虚设、
缺少三合一校验与原子重启」的活样本。

### 1.2 现状复核（2026-08-06 00:09 · master @ 033e4a85）

| 项 | 实测值 | 判定 |
|---|---|---|
| python 进程 | 仅 PID 38456（系统 Python，08-05 18:11 启动） | ⚠️ 单进程但仍是系统 Python，非 venv |
| 8000 端口 | netstat 无监听 | ❌ 引擎疑似未起/已退出 |
| 日志 | 08-05 19:52–23:53 约 20 次 `mode=dry_run` 启动 banner；traceback 均指向系统 Python310 | ❌ 多条 dry_run 链反复起停（P1-2 实证） |
| connect bot | 23:51/23:53 反复拉起 5 bot，stop 多次 taskkill 30s 超时 | ❌ 生命周期未解耦（B1 目标） |
| miniQMT 客户端 | XtMiniQmt PID 44044 在 | ✅ 进程在 |
| SSoT A/B/C | `live_trades.csv`/`record_live_trade` 已删，`audit_ssot.py` 已建 | ✅ 已提交 17a66588..033e4a85 |

结论：进程拓扑从 08-05 的「三条链并存」变为 08-06 的「单系统 Python 进程 + 8000 无监听」，
但根因（无单一所有者、系统 Python 启动路径、dev/dry_run 实例反复起停）未变——
Phase B1 仍是当前最优先，且 08-06 09:22 若沿用旧代码，session 关误拦将再次发生。

---

## 2. Phase A：风控废弃删除（决策留痕与边界）

> 决策来源：`docs/superpowers/plans/2026-08-05-risk-controls-removal-pending.md`
>（研究员 Annotation 1，仅留痕未实施）；实施蓝图见
> `docs/superpowers/specs/2026-08-05-risk-controls-removal-design.md`（DRAFT）。
> 本 spec 将其正式并入总纲作为 Phase A，与 Phase B 共同服务「打通自动化链路」。
>
> ⚠️ DRAFT 内部行号已漂移（如 risk.py:81 → 现 :140、trading_service.py:469 → 现 :500），
> 实施 Phase A 前按符号名（Grep/codegraph）刷新定位，勿按行号改。

### 2.1 触发背景（与 §1.1 同一天的事故）

2026-08-05 09:22 pre_open 挂单 300358.SZ 5700 股 @8.75 被「非 A 股交易时段」闸拦截
（`_in_a_share_session()` 只认 09:30–11:30 / 13:00–15:00，不含集合竞价；pre_open 调度在
09:22），订单从未送达 QMT 柜台。研究员据此要求：**废弃删除当前风控所有逻辑，待链路调通后
整体重构——不是逐个打补丁**。

### 2.2 删除清单（汇总，明细见 DRAFT §2）

| 层 | 删除项 |
|---|---|
| 下单挡板（10 关） | connection / dry_run / allow_live / confirm / whitelist / lot / max_amount / max_shares / high_low_limit / session（`trading/compute/risk.py`） |
| 网关/熔断层 | 断线保护锁、账号状态熔断、emergency_halt、日内 -3% 熔断（`broker/qmt.py` / `trading/compute/breaker.py` / `trading/io/breaker.py`） |
| 引擎流程闸 | pre_open 三段 gate、_gw_health_gate、veto、max_wait、_CriticalHalt、stop_loss 时段闸、pipeline 数据闸 |
| 计划/人审 | plan.confirmed 确认闸（SSoT C 后读 `trade_event CONFIRMED/VETOED`；save/confirm_plan 已删）、veto 保护逻辑 |
| 启动/部署 | 影子期硬闸 check_shadow_gate、QMT session 单实例锁、端口 8000 单例 |
| 白名单 | 静态 QMT_SYMBOL_WHITELIST + 动态白名单机制 |
| 订单状态机/幂等 | OrderStateMachine 非法迁移拒绝、has_order 死态重挂许可、job_ledger 防双跑 |
| 配置项 | QMT_ALLOW_LIVE_TRADE / QMT_ENFORCE_SESSION / QMT_ORDER_MAX_AMOUNT / QMT_ORDER_MAX_SHARES / QMT_SYMBOL_WHITELIST / TRADE_SHADOW_MIN_DAYS / CIRCUIT_DAILY_LOSS_LIMIT / AUTO_TRADE_MODE / AUTO_CONFIRM_PLAN |

### 2.3 与 Phase B 的分工和红线（本 spec 的合并立场）

- **分工**：Phase A 管「让单出得去」（删拦截、修 09:22 挂单）；Phase B 管「让链稳得住」
  （单一进程所有者、网关自愈、miniQMT guard、测试/生产隔离）。A 是目标，B 是底座。
- **红线 1（先通后删）**：Phase A 批量删除的前置 = §7.1 链路调通验收全过；未过验收前，
  只允许对 session 关做最小修复（上午起点 09:30→09:15 或删除 session 关），不得批量删除。
- **红线 2（正确性原语默认保留）**：决策记录要求「全删」，但 DRAFT §6 Q1 未决——本 spec
  默认把「链路正确性原语」划归 Phase B 保留：QMT session 单实例锁、端口 8000 单例、
  成交/订单幂等、审计事件、断线保护。**是否连这些也删，实施前由用户裁定**；默认不删，
  因为删了会重蹈 07-29 双进程抢 session / 重复成交的覆辙。
- **红线 3（可回滚）**：Phase A 每步独立提交、独立回滚，禁止一次性大删（DRAFT §7）。

### 2.4 未决问题（Phase A 实施前必须裁定）

1. 「所有风控」是否包含断线保护、幂等、审计？若全删，防重复挂/防幽灵单靠什么？
2. 删除后 09:22 集合竞价挂单是否恢复为唯一目标（session 问题自然消失）？
3. 白名单删除后，前端手动下单路径的标的约束是否也放开？
4. 影子闸删除后，LIVE 启动是否无条件放行？
5. 日内 -3% 熔断删除后，异常回撤靠什么兜底（人工盯盘？）？
6. 「链路调通」的验收标准以 §7.1 为准，由谁验收、何时验收？
7. 删除过程中是否需要 dry_run 过渡期？

---

## 3. 目标架构（一句话）

**一个超级管理器（Windows 计划任务）→ 只拉起一个引擎进程（venv `python -m trading`）→
引擎只连一个由独立看门狗保活的 miniQMT 客户端 → 测试/开发进程与生产端口彻底隔离。
所有「新/旧进程」由唯一所有者判定，不再靠猜测。**

```
QuanterSupervisor (schtasks ONSTART)
  ├─ 顺序：① miniQMT 客户端看门狗 → ② TradingEngine（venv, 单实例）
  ├─ 持有：pid 文件 + 端口 8000 + session 锁 三合一校验
  └─ 异常：看门狗负责杀旧启新（带告警），绝不允许第二个引擎

miniQMT Guard（独立 5min 任务）
  ├─ 客户端进程不在 → 启动 XtMiniQmt.exe + 登录就绪探测
  ├─ 进程在但文件陈旧/未登录 → 钉钉 WARN（不假装活）
  └─ 残留 session 队列（down_queue_win_*）→ 引擎 connect 前自动清理（W1.3 已做）+ guard 兜底

TradingEngine（唯一消费者）
  ├─ bootstrap：客户端就绪 gate → connect（返回码权威，W1.1 已做）
  ├─ 断线：health_guard 60s 自愈 + 首轮告警（W1.2 已做）
  └─ 不退出：网关 loss 只锁单，不自杀（既有设计）
```

---

## 4. 三个域怎么根治

### 4.1 进程域：单一所有者 + 三合一校验 + 原子重启

| 现状问题 | 最终方案 |
|---|---|
| 多条启动链并存（schtasks / 手动 venv / 系统 Python / uvicorn reload / dev 工具） | 唯一入口 `scripts/start_server.bat`（已存在）；所有启动/重启必须走 `ops/trading_supervisor.py` |
| 旧进程常驻、端口被新进程顶掉 | 启动时三合一校验：**端口 8000 属主 PID == pid 文件 PID == session 锁持有者**，不满足即拒绝并告警，绝不「顶掉」 |
| 重启时机混乱（00:51 那次就是手动/他因启动） | `ops/restart_trading.py` 提供原子「停旧树→启新」唯一操作，人工/脚本都用它 |
| 系统 Python 启动路径 | 删除系统 Python 启动路径；bat/脚本强制 venv 绝对路径；supervisor 对非 venv 引擎进程标记「非法链」 |

三合一校验的读取方式（Windows 现实约束，诚实标注）：

- **端口属主 PID**：`netstat -ano` 解析 `:8000` 的 LISTENING PID（ops 层允许 subprocess；
  交易进程内仍用 socket 探测，不引入 psutil）。
- **pid 文件 PID**：`logs/trading_engine_<session>.pid` 首字段（`trading/single_instance.py`
  已写，含 pid + ISO 时间戳）。
- **session 锁持有者**：`single_instance.acquire(session)` 返 None = 锁被持有；返锁对象 =
  锁空闲（立即 release，探测不抢锁）。锁文件本身存 `\0` 不存 pid，pid 以 `.pid` 文件为读口。

一致性判定：三者同 pid → OK；端口空闲但锁被持有 → 引擎进程已死但锁未释放（OS 会自动释放，
通常不会出现；出现即告警）；pid 文件与端口不一致 → 旧链残留，拒绝新实例并告警。

### 4.2 网关域：状态模型升级 + 观测端点

| 现状问题 | 最终方案 |
|---|---|
| 客户端掉线/重启后引擎被动重连；status 只反映「连没连上」 | 状态模型升级为 5 态：`client_down / client_stale / disconnected / live / vetoed`（前端/播报按此展示） |
| health_guard 重连成功/失败无客户端诊断上下文 | W1.2 已做（WARNING + `_client_staleness_diag` + 限流钉钉） |
| 无进程拓扑一屏视图 | 新增 `GET /api/v1/ops/processes`：引擎 pid/端口/锁持有者/客户端 pid/队列大小/网关态，漂移即告警 |

### 4.3 miniQMT 域：独立看门狗 + 自动登录 + 队列兜底

| 现状问题 | 最终方案 |
|---|---|
| 客户端进程在但没登录/文件陈旧；引擎无法自动重启客户端 | 独立 `ops/miniqmt_guard.py`：进程不在 → 拉起 XtMiniQmt.exe linkMini；进程在但 quoter 目录/登录文件陈旧 → WARN + 钉钉（不误杀、不假装活） |
| 重启时机混乱 | 客户端配置启用「自动登录」（人工勾一次，之后重启免输入）；启动顺序强制 guard → 引擎就绪 gate |
| session 队列残留 | 引擎 connect 前清 `down_queue_win_{sid}`（W1.3 已做）；guard 兜底清残留 |

### 4.4 session 自动化修正：查询渠道 + 三级自愈（2026-08-05 用户追问后对齐）

**查询渠道（全部可自动化）**：

1. **userdata 目录 = sid 占用登记表**：`down_queue_win_{sid}` / `lock_*queue_win_*` 列出
   所有活跃/残留 sid（`scripts/qmt_clear_session_lock.py` 已实现扫描，`broker/qmt.py`
   的 `_cleanup_session_files` 同源）。
2. **connect 返回码 = 权威探测**：0=成功 / -1=该 sid 被占用（`broker/qmt.py` 已实现）。
3. **客户端进程/登录文件 = guard 探测**（Phase B2 实现）。

**修正策略分三层（自动优先，人工兜底）**：

| 层级 | 触发 | 动作 | 结果 |
|---|---|---|---|
| L1 首选 | `.env QMT_SESSION_ID`（preferred sid）未被占用 | 直接用 preferred sid | 零漂移，无任何告警 |
| L2 自动规避 | preferred sid 被占用 / connect 返 -1 | 自动轮换到「未出现在 userdata 的 sid」（preferred 起有界递增搜索）→ 清理 → 重连；成功后将**实际 sid** 写入 runtime SSoT（`logs/engine_session.json` + `state_store.account.session_id`），INFO/WARN 记录 preferred→actual | 故障自愈，`.env` 不变 |
| L3 人工兜底 | 轮换后仍失败（非 -1 环境错误 / 客户端未登录） | 钉钉 + fail-closed（拒绝 connect） | 人工核实后 `restart_trading.py --adopt-client-session` 固定新 preferred |

**为什么 `.env` 可以不变**：session_id 不是账户标识（账户由 `QMT_ACCOUNT_ID` 独立指定），
只是隔离键——自动选一个未占用 sid 不会连错账户，只可能撞 sid（已被 L2 扫描排除）。
因此「自动修正」是安全的；之前担心的「自动改 `.env` 掩盖真根因」只针对**持久化配置改写**，
不针对**运行时 sid 选择**。`.env` 保留为 preferred（引擎身份、进程锁键、观测锚点），
实际 sid 以 runtime SSoT 为准，banner/观测端点同时展示两者，漂移可见但不阻断。

**进程锁键仍用 preferred sid（引擎身份）**，轮换的只是 trader 会话 sid——两个引擎同时启动
时仍被单实例锁串行化，不会同时扫到同一个空闲 sid 造成双抢。

---

## 5. 测试/开发与生产隔离

- `trading/__main__.py` 增加 `QUANTER_TESTING=1` 环境：跳过单实例/端口断言，测试用临时端口
  （或 mock uvicorn.run）。
- `pytest.ini` 默认排除会真起服务器的用例（现有 `e2e_long` 已排除；`test_main` 端口用例已 mock
  端口探测；新增 `server_lifecycle` marker 统一收口）。
- `ops/dev.py` 前后端启动统一走 supervisor 语义（后端入口改为 `python -m trading`，复用端口
  探测与 .env 加载），`QUANTER_DEV_SKIP_CONNECT_BOTS=1` / `QUANTER_TESTING=1` 时 lifespan
  不随服务器重启 5 个 connect bot（dev 不再被 bot 生命周期绑架）。

---

## 6. 落地阶段（A 打通挂单 → B 打通链路 → C 收敛真相源）

### Phase A：风控废弃删除（前置 = §7.1 链路调通验收）

1. 决策冻结：`2026-08-05-risk-controls-removal-pending.md` 已并入本 spec §2。
2. 链路调通验收（Phase A 前置，验收项见 §7.1）：
   - pre_open 09:22 能成功挂单（session 关不再误拦：删除或上午起点改 09:15）；
   - 成交回报回流 order/fill/position，无幽灵单、无重复挂；
   - stop_loss / 止盈 / 撤单 / 对账 / 重启补跑全链路 live 跑通；
   - P0-2：台账 partial/failed 语义 + C-8 窗口幂等重试（不再用 done 掩盖 0 成交）；
   - P0-3：代码版本与进程启动时间可查，更新后未重启能告警；
   - P1-1：QuanterServer 为 ONSTART 且有 RestartOnFailure；
   - P1-2：生产启动 fail-closed，dry_run 实例不污染生产日志/端口；
   - P2-2/P2-3：单笔拒因进台账 message；告警成功/失败可审计。
   - P0-4（新增）：audit_ssot 进程拓扑三项全绿（§7.1-9）。
3. 按 DRAFT Phase 2 顺序删除：配置层 → 挡板层 → 引擎闸 → 熔断层（未决）→
   启动层（未决）→ 状态机（未决）；每步独立提交可回滚。
4. 未决问题（§2.4）实施前裁定；裁定结果回写本 spec，不留口头决定。

### Phase B：进程·网关·miniQMT 一体化治理（本 spec 主体）

#### B1（1–2 天）：进程单一所有者 + 测试隔离

1. `ops/trading_supervisor.py`：状态/启动/停止 + 三合一进程校验（端口、pid 文件、session 锁）。
2. `ops/restart_trading.py`：原子「停旧树→启新」唯一操作（默认 dry-run 展示，`--yes` 才执行）。
3. `trading/__main__.py` + `trading/engine.py`：`QUANTER_TESTING=1` 跳过单实例/端口断言。
4. `pytest.ini`：新增 `server_lifecycle` marker 并默认排除。
5. `ops/dev.py`：后端统一走 `python -m trading`；`QUANTER_DEV_SKIP_CONNECT_BOTS=1` 跳过 connect bot。
6. `presentation/server/main.py` lifespan：测试/dev 环境跳过 connect bot 装配。
7. 清掉当前残留 python 进程（系统 Python 27592、venv 70848、系统 Python 79572 等），按新拓扑只留一条链。
8. 更新 08-04 runbook：启动/重启一律走 `restart_trading.py`。
9. P0-3 落地：启动日志打印 git 版本/进程启动时间 + 「代码已更新但未重启」告警。

#### B2（本周）：miniQMT 看门狗 + 观测端点

1. `ops/miniqmt_guard.py`（5min 任务：拉起客户端 / 陈旧 WARN / 队列兜底）。
2. 客户端「自动登录」配置 SOP + `restart_trading.py --adopt-client-session` 半自动修正通道。
3. `GET /api/v1/ops/processes` 观测端点 + 钉钉告警。
4. §4.4 L2 sid 自动轮换：preferred 被占 / connect -1 → 自动换未占用 sid 并记 runtime SSoT。

#### B3：真相源收敛（SSoT 合并）

- **已完成（2026-08-06 复核）**：SSoT A/B/C 已提交（17a66588..033e4a85）——CSV 写/读路径
  删除、消费端切 DB、expired/param_iter/daily_equity/播报幂等/计划内容全部收口。
- **剩余缺口**：
  - `scripts/audit_ssot.py` 已有 `check_engine_process_count`（引擎数 ≤ 1），
    补「客户端进程 = 1、端口属主一致」两项；
  - audit_ssot 当前用 `wmic` 取命令行（本机实测可能超时/RPC 失败），
    进程拓扑检查改用 `netstat -ano` + `Get-Process`（与 supervisor 同源），不依赖 wmic。

---

## 7. 验收标准（「不再出现」的定义）

### 7.1 链路调通验收（Phase A 前置）

1. pre_open 09:22 集合竞价窗口可成功挂单（session 关不误拦）。
2. 成交回报回流 order/fill/position，无幽灵单、无重复挂。
3. stop_loss / 止盈 / 撤单 / 对账 / 重启补跑全链路 live 跑通。
4. 台账不再用 done 掩盖 0 成交：pre_open 返回 submitted/rejected，失败可在 C-8 窗口内幂等重试。
5. 代码版本与进程启动时间可查；代码更新后未重启能告警。
6. QuanterServer 为 ONSTART 且有 RestartOnFailure。
7. 生产启动 fail-closed（AUTO_TRADE_MODE 必须显式 live），dry_run 实例不污染生产日志/端口。
8. 单笔拒因进台账 message；告警成功/失败可审计。
9. `scripts/audit_ssot.py` 进程拓扑三项全绿：引擎数 = 1、客户端进程 = 1、端口属主一致。
10. connect bot 生命周期可观测且启停不拖垮链路（B1 解耦后 dev 不再随服务器重启 5 个 bot；
    `connect_manager.stop` 的 taskkill 超时降级修复并告警）。

### 7.2 最终验收（A + B 合并）

1. 任意时刻 `Get-Process python*` 中 `-m trading` 恰好 1 个（venv 链）。
2. 8000 端口属主 == pid 文件 == 锁持有者，三值一致。
3. 重启只通过 `restart_trading.py` 一条路，旧进程树必被清干净（无 orphan）。
4. miniQMT 进程不在时 5 分钟内被拉起；在但未登录时 5 分钟内出 WARN，不再出现「网关静默断 9 小时」。
5. 全量 pytest 与生产服务器互不干扰（可同时跑）。
6. 自动化链路全天候可跑：计划 → 挂单（含集合竞价）→ 成交回报 → 止损/止盈 → 对账 → 重启恢复，无人工介入。

---

## 8. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 三合一校验误杀合法 schtasks 链 | supervisor 只告警/拒绝新实例，绝不自动 taskkill；`restart_trading.py` 默认 dry-run，`--yes` 才动手 |
| 重启旧树时误杀无关 python | 只匹配「exe 为项目 venv」或「命令行含 `-m trading` / `presentation.server.main:app`」的进程；dry-run 先展示清单 |
| `QUANTER_TESTING=1` 被误带入生产 | env 仅测试显式设置；`run_server` 只在 testing 时跳过断言，生产启动链不设置该变量 |
| dev 切 `python -m trading` 后 .env override 改变行为 | dev 本来就要读 .env；如出现差异，可用 `--reload` 开关与 `QUANTER_DEV_NO_RELOAD=1` 控制 |
| 测试跳过单实例后双引擎 | 仅影响 pytest 进程；测试不 bind 8000（mock uvicorn.run 或临时端口） |
| Phase A 链路未通就删风控 | 真金裸奔 | §2.3 红线 1：先过 §7.1 验收再删；未过验收只修 session 关 |
| 系统 Python 直跑 dev/测试实例反复起停（08-05 日志 19:52–23:53 约 20 次 dry_run 启动） | 日志污染 + 可能抢占 8000/session | B1 强制 venv 绝对路径 + supervisor 对非 venv 引擎进程标记非法链；dev.py 统一走 `python -m trading` |
| `connect_manager.stop` taskkill 超时（08-05 23:52/23:53 实证 30s TimeoutExpired） | 停机慢/进程残留 | B1 dev 解耦 + B2 观测端点；stop 超时软降级清 PID 文件（已有行为）并告警 |

回滚：每个 Phase 独立 commit；Phase B1 可整体 revert（supervisor/dev 改动不影响既有 `start_server.bat` 链路，
`QUANTER_TESTING` 缺省不生效）。

---

## 9. 附录：W1.x 已落地清单（本 spec 不重复实现）

- W1.1 `is_client_ready`：userdata 目录在即 True，connect 返回码唯一权威（`broker/qmt.py`）。
- W1.2 `_health_guard` 未就绪 WARNING + 限流钉钉（`trading/engine.py`）。
- W1.3 connect 前置清 `down_queue_win_{sid}`（`broker/qmt.py`）。
- W1.4 `_assert_single_instance` 端口占用 CRITICAL + exit（`trading/__main__.py`，Phase B1 将其并入三合一）。
- QMT session 单实例锁 + pid 文件（`trading/single_instance.py`）。
