---
title: quanter 全项目审计与治理 spec（G/A/C/P2-ts 十四工单）
date: 2026-08-13
revision: v1（落盘稿·待批准 → writing-plans）
baseline: HEAD 47561049（2026-08-13）
status: draft（待用户批准）
author: session（peer reviewer / risk officer 姿态）
scope: 全项目深度审计落盘 + 14 治理工单详设（保护链 G1-G7 / 策略可信 A1-A5 / 配置 C1 / 时间戳 P2-ts）
related:
  - docs/superpowers/specs/2026-08-12-overall-optimization-design.md（总纲·策略与参数优化 P0-P6，A2/A3/A5 与之交叉）
  - docs/architecture/06-tech-debt.md（债务切片，§8 同步订正）
  - docs/superpowers/specs/2026-08-11-tech-debt-governance-master-design.md（技术债总纲）
---

# 全项目审计与治理 spec

> 本 spec **不改代码**；改造由后续实施计划（writing-plans）落地。G 波（保护链）本周可动工，A 波与 opt/p1-vectorization 轨道耦合，待 P1/P5 节奏明确后切入。
> 本 spec 是 2026-08-13「全项目深度审计」（11 路并行只读 + 关键断言亲验）的落盘归宿，**含审计原报告的 4 处事实订正**——防后人引用带水分的结论。

---

## §0 审计复核结论

### 0.1 方法

11 路并行只读审计（错误处理 / 并发 / 安全 / 测试 / 配置漂移 / 代码重复 / 数据一致性 / 后端 API / 前端工程 / 算法策略 / 模块架构）+ 关键断言亲验（CI 路径、P0-3 机制、re-export 占比）。作为 risk officer 角色下判断，单源推断标注 [PLAUSIBLE]，跨切片独立印证标注【印证】。

### 0.2 四处事实订正（原审计报告夸大处，如实写明防误引）

| # | 原结论 | 订正 | 证据 |
|---|---|---|---|
| ① | P0-3 pre_open 双挂单竞态 = **Critical** | **降级 Low**。机制核实：`_pre_open`(cron, engine.py:1390) 与 `_catchup_pre_open`(catchup.py:156) 同住单事件循环；catchup 入口 `latest_status("pre_open")`(catchup.py:143-145) 与 `pre_open` 内 `begin_run` 均同步 sqlite、无 await 让出点，「检查→置 running」单线程内原子完成，后到协程必见 running 跳过；叠加 `has_order` purpose 幂等(state_store.py:919) + 进程级 `_instance_lock`(engine.py:1656)。原 [PLAUSIBLE] 评级仍过头 | 亲验调用链 |
| ② | engine.py「约一半是 re-export 垫片」 | **实为 ~12%**（含 Why 注释的整个 re-export 区域 ~200/1660 行；纯 import 语句 ~25 行 ≈1.5%）。orphaned 那 5 个零引用**属实**，但占比表述错 | 实数 engine.py:65-263 |
| ③ | 前端「研究动线三屏全断」 | **两屏断 + 一屏空**。/caisen、/lab 确实 404 全断（核心结论不变）；/dashboard 是连通但 sector 湖退役恒空，不属「断」 | 前端审计 |
| ④ | （定性）多数据源 | **印证，非修正**。多源（tushare/QMT/akshare/FRED/yfinance）有明确边界、是设计非债——本审计第一轮即如此定性 | — |

> 订正原则：审计整体方向成立，但 P0-3 评级与 re-export 占比两处有实质夸大，前端三屏有表述水分。对抗性复核必要——审计本身也需被复核。

### 0.3 复核新发现 N1-N5（收编进工单）

| ID | 内容 | 归宿 |
|---|---|---|
| **N1** | 本机 `.venv310` 缺 pyarrow/optuna/yfinance → `pytest --collect-only` 14 个测试文件收集报错（test_integrity/test_repair_gaps/test_freshness 等 ModuleNotFoundError）。requirements.txt 有此三包，CI 装齐不受影响；但**本机 `python ops/run_checks.py` gate③ 现在就是红的**——这是「CI 复活即红」真源头。必须先 `pip install -r requirements.txt` 再改 ci.yml | **G1 前置** |
| **N2** | `trading/io/orders.py:29` 仍带 `confirm: bool = True`，:44 透传给已删该参的 `gateway_service.submit_order` → 调用即 TypeError。当前零消费者（仅 re-export），潜伏 bug 非 live bug | P2-W2 顺带清理 |
| **N3** | 出场侧硬耦合漏列第 4 处 `trading/compute/__init__.py:43`（re-export ExitAction/ExitReason）；审计误列的 `plan.py:33` 实为进场侧 Signal，应剔除出「出场侧」计数 | P2-W2 顺带清理 |
| **N4** | 旧挂账「gateway_service 日志旧名（trading_service）stale 注释」已被 `96d55418`(08-12) 修复——闭项，非待办 | 文档订正（闭项） |
| **N5** | tech-debt 漏记两条真债：① trailing 动态收紧删除（engine.py:433 自承「live P0 待重做」）② `run_data_check.py:18/91` data→trading 反向依赖——代码俱在，06-tech-debt.md 均无条目 | §8 同步补 |

---

## §1 对齐声明（三轨交叉映射）

### 1.1 三轨定义

- **轨 A（本审计）**：2026-08-13 全项目深度审计 → 本 spec 的 G/A/C/P2-ts 工单
- **轨 B（总纲）**：`overall-optimization-design.md` P0-P6（策略与参数探索优化）
- **轨 C（债务）**：`06-tech-debt.md` + `tech-debt-governance-master-design.md`

### 1.2 交叉映射表

| 治理点 | 处置 | 理由 |
|---|---|---|
| **A2 walk-forward** | **并入轨 B 的 P5**（不独立立项） | P5 已含 walk-forward 多折（折含 2022 熊市）+ 分折 universe 重建；本 spec 仅补「各年 min calmar」决策门（DG-G4）作为 P5 的排序约束 |
| **A3 回测真实性** | 引用轨 B，但 A3 的流动性/停牌/涨跌停建模是策略层、总纲未覆盖 | 半独立：方法论属 A，落地与 P1 向量化同船 |
| run_data_check 反向依赖 | 引用轨 C（§8 补 tech-debt 条） | N5②，非新立项 |
| 前端 caisen 死视图 | 引用轨 C（tech-debt Low 既有项） | 不重复立项，G 波不含 |
| re-export 垫片清理 | 引用轨 C（W1-A/T2 follow-up「silently orphaned patch」既有项） | 不重复立项 |
| **account_daily 闭环表述** | **显式订正**（见 1.3） | 总纲 §8 实为参数闭环；account_daily「闭环」表述疑在 tech-debt T13 条——08-11 治了静默（告警+T-1 兜底），**fail-open 语义残留，G3 收尾** |
| **G1-G7 / A1 / A4 / C1 / P2-ts** | **新增治理点**（轨 B/C 未覆盖） | 本 spec 主体 |

### 1.3 account_daily 订正

> ⚠️ 待订正时确认确切出处（总纲 §8 是参数闭环，不含 account_daily；疑似 tech-debt T13 治理条的乐观措辞）。
> **订正口径**：account_daily.start 漏采 → 08-11 已治「静默」（告警 + T-1 兜底），但 `breaker.py:50` `if start_equity <= 0: return False` 的 **fail-open 语义残留**（基线 NULL 时不熔断）→ **G3 收尾**（fail-closed + 基线修复同落地）。非「已闭环」。

---

## §2 保护链波 G1-G7（本周动工 · 分支 `fix/p0-guards`）

> 物理隔离于 `opt/p1-vectorization`。每工单：基准(file:line @ HEAD 47561049) + 治疗 + 验证 + 对抗推演。

### G1　CI 复活（前置 N1）
- **基准**：`.github/workflows/ci.yml:43,51,54`；`ops/run_checks.py`
- **治疗**：① **前置（N1）**：本机 `pip install -r requirements.txt` 补 pyarrow/optuna/yfinance，确认 `python ops/run_checks.py` 5 gate 绿；② 改 ci.yml：`web`→`presentation/web`（:43 cache-dependency-path、:51 npm ci --prefix）；`scripts/run_checks.py`→`ops/run_checks.py`（:54）；③ 加自检 step `test ! -f scripts/run_checks.py` 防再次漂移。
- **验证**：push 后 CI 4 gate 实跑且绿；本机 run_checks 与 CI 同源同绿。
- **对抗推演**：未来 scripts↔ops 再迁移又会漂移 → 自检 step 兜底；另 npm prefix 改 presentation/web 后，cache-dependency-path 也要同步否则 cache miss。
- **决策门**：DG-G1 ✅ 定稿（先本机跑绿再改 ci.yml）。

### G2　鉴权 fail-closed + SSE 鉴权
- **基准**：`presentation/server/http/auth.py:58`；`presentation/server/main.py:644,650-662,674`；`start_server.bat`
- **治疗**：① `require_write` live 模式（`AUTO_TRADE_MODE=live`）token 未配 → fail-closed（startup 拒起或写端点返 503），dry_run 允许无 token；② `run_server` 默认 `host=127.0.0.1`，外网显式放开；③ `/logs/stream` 挂鉴权——**优先 cookie 方案**（同源自动携带、不进 URL），query token 仅内网 fallback 且 access log 脱敏；④ `/health` 去 version 字段。
- **验证**：live 无 token 拒起；dry_run 无 token 可起；SSE 带 cookie 可订、无 cookie 401；新增 `test_auth_fail_closed_live`。
- **对抗推演**：fail-closed 后生产忘配 token → engine 拒起业务中断 → start_server.bat 醒目报错 + dry_run 豁免兜底；cookie 方案要求前后端同源（Vite 代理已是同源 ✓）。
- **决策门**：DG-G2 ⚠️ 定稿（**cookie 优先，非 query token**——避免 token 进 access log）。

### G3　熔断 fail-closed + account_daily 基线（与 G3 同体落地）
- **基准**：`trading/compute/breaker.py:50`；`account_daily.start_total_asset` 漏采（pre_open 基线抓取）
- **治疗**：① `breaker.py:50` 基线 NULL/≤0 改 fail-closed——模拟盘=熔断停手+CRITICAL 告警，实盘=`raise _CriticalHalt`（不选「仅告警」）；② **基线修复同体**：pre_open 抓 start 基线时若无 account_daily → T-1 收盘快照兜底 + 写入，避免「每天 fail-closed 停手」。
- **验证**：注入基线 NULL → 模拟盘停手+告警、实盘 _CriticalHalt；基线修复后正常不熔断；`test_breaker_fail_closed` + `test_baseline_backfill`。
- **对抗推演**：基线修复若不同体落地，fail-closed 退化成「每天开盘熔断」→ 二者必须同 PR；T-1 兜底若 T-1 也漏采 → 链式，须有「连续 N 日无基线 → 拒起」硬闸。
- **决策门**：DG-G3 ✅ 定稿（fail-closed；模拟盘停手+告警，实盘 _CriticalHalt）。

### G4　外部 SDK 超时注入（韧性链复活）
- **基准**：`data/_tushare_compat.py:78 get_pro`；`data/fetcher.py:614 Fred`；`data/clients/yfinance_client.py:79`；`broker/qmt_quote.py:148 get_full_tick`；`data/calendar.py:44 trade_cal`
- **治疗**：凭证入口统一注入 timeout——`get_pro()`/`Fred()` 用线程池 `future.result(timeout=30)` 包裹；`get_full_tick` 的 `run_in_executor` 加 `asyncio.wait_for(timeout=5.0)`；trade_cal 启动期同步调用改线程池。让 TCP 挂起抛 TimeoutError → 触发 CircuitBreaker.record_failure。
- **验证**：mock socket 挂起 → 30s/5s 抛 TimeoutError → breaker 计 failure；正常调用不受影响。
- **对抗推演**：timeout 过紧误杀慢响应 → 阈值取 akshare(30s)/alpha_vantage(15s) 已验证量级；xtdata 5s 盘中若频繁超时 → 降级跳过（与现「行情缺失跳过」语义一致）。
- **关联**：对齐 akshare(`_call_ak` 30s)/alpha_vantage(httpx 15s) 已有范式，消除三重标准。

### G5　数据原子写 + schema 迁移安全
- **基准**：`data/tools/sync_daily_incremental.py:258`；`data/tools/repair_gaps.py`；`trading/state_store.py:170`(DROP 重建)
- **治疗**：① `safe_overwrite` 升级 tmp+rename 原子语义（`to_parquet(LAKE+".tmp")` → `os.replace`），sync_daily_incremental/repair_gaps 接入；② state_store 旧 fill/position 表 DROP 重建改「导出→重建→回灌」备份式迁移，破坏 live-前-无-数据 假设前先备份。
- **验证**：注入写中途异常 → 目标 parquet 不损（旧文件在）；旧表迁移后行数守恒；`test_atomic_overwrite` + `test_schema_migration_preserves_rows`。
- **对抗推演**：tmp 文件残留 → 写前清 .tmp；os.replace 跨设备失败 → 保证 tmp 与目标同卷。

### G6　SQLite 协调点韧性基线
- **基准**：`trading/job_ledger.py:57`；`trading/state_store.py:60`；正范式 `backtest/tasks_db.py:45`、`discovery/store.py:50`
- **治疗**：`_connect` 统一 `timeout=30` + `PRAGMA journal_mode=WAL`，对齐 tasks_db/discovery_store。job_ledger 的 begin_run 领取类操作加 `BEGIN IMMEDIATE`（即便 P0-3 双挂单不成立，连接韧性仍是真 Medium）。state_store docstring 钉死「仅事件循环线程可写」红线。
- **验证**：并发写压测无 SQLITE_BUSY 抛错（WAL 串行化）；`test_sqlite_wal_baseline`。
- **对抗推演**：WAL 下 -wal/-shm 文件需随库备份；timeout=30 仍 busy → 返回错误而非静默（可观测）。

### G7　告警可观测 + FSM 收口
- **基准**：`broker/qmt.py:224,1351,1386,1394,1400`(告警 pass)；`discovery/worker.py:63`、`daemon.py:137,142`、`snapshot.py:105`、`objective.py:46`；`trading/state_store.py:419,939,960` vs `order_state.py` OrderState
- **治疗**：① 告警 fire_and_forget 外层 `except: pass` 加 `logger.debug/warning`（消除「监控监控器」盲区，不改控制流）；② state_store 写状态过 `OrderStateMachine._is_valid_transition` 校验，`_TERMINAL_ACTIONS`/`_PENDING_ORDER_STATES` 改引用 order_state 单源（消三套终态集漂移）。
- **验证**：告警通道失败有 log；非法状态迁移被拒；`test_fsm_guard_on_write` + `test_terminal_set_single_source`。
- **对抗推演**：FSM 校验拒绝合法但对账触发的迁移（如 broker 异步回报）→ 白名单「broker 回调直写」通道，校验仅拦异常序列。

---

## §3 策略可信波 A1-A5（与 opt/p1-vectorization 耦合，待 P1/P5 节奏后切入）

> **前置裁定**：颈线法当前可信度低（regime 过拟合 + 4% Kelly 薄边缘 + 回测理想化 + 风控单点失效），A 波完成前**仅作研究/模拟盘**。

### A1　市场状态闸接入
- **基准**：`backtest/tools/market_regime_filter.py`（诊断脚本，生产零接）；`discovery/judging.py:13`(L0 闸无熊市否决)
- **治疗**：产品化 regime 闸——沪深300 200日均线 + 市场宽度双确认，空头环境停手/降仓。接入 engine._eod 选股前置 + plan.py 下单前置。
- **红线**：regime 阈值（均线天数/宽度）**绝不进 TPE 搜索空间**，固定经验值或独立样本定，否则过拟合 regime 参数。
- **决策门**：DG-G4 ✅ 定稿（200日均线+宽度双确认；空头先停手；inner 改各年 min calmar）。

### A2　walk-forward（并入轨 B P5，不独立立项）
- 治理归宿：`overall-optimization-design.md` §6 P5（折含 2022 熊市 + 分折 universe 重建）。本 spec 仅贡献 **DG-G4「inner 改各年 min calmar」** 作为 P5 排序约束。

### A3　回测真实性
- **基准**：`strategies/neckline/backtest.py:175`(simulate_exit 全量成交)
- **治疗**：补①挂单量≤当日成交量×k ②停牌日标记（max_holding 按 bar→按交易日）③跌停封板止损卖不出建模。落地与 P1 向量化同船。

### A4　分数 Kelly + 集中度
- **基准**：`backtest.py:381`(pos_cap=5%)；`backtest/models.py`(max_positions=6 无行业维度)；`trading/compute/risk.py:50`(删到 3 闸)
- **治疗**：① 分数 Kelly 0.25×（≈1%/笔）起步，上限 0.5× 需样本外验证；② 行业/主题并发上限（每行业≤2 仓）；③ 组合回撤止损（X% 停策略 N 天）。
- **决策门**：DG-G5 ✅ 定稿（0.25× 起步，上限 0.5× 需样本外验证）。

### A5　universe 生存偏差
- **基准**：`trading/data_ctx.py:37`(load_universe 仅前缀过滤)；`backtest.py:550`(fullscan 当前快照)
- **治疗**：时点可交易集（point-in-time membership）+ ST/*ST/退市过滤 + 上市≥250 日 + 信号日时点流动性过滤。

---

## §4 配置 C1（默认值漂移收口）

- **基准**：`trading/compute/plan.py:98`(stop_atr_mult 2.0)、`:104`(tp1_portion 0.0)；`trading/critical.py:229-230`(trailing grace/step env 5/0.1)；`trading/eod_plan.py:119`(stale 注释 0.75)；价位公式三份 `backtest.py:148`/`method_v0.py:328`/`plan.py:139`
- **治疗**：① 兜底默认对齐——plan.py stop_atr_mult→1.0、tp1_portion→0.5；trailing env 默认→0/0.0（或删 env 直到 follow-up）；eod_plan 注释→1.0；② **抽 `compute_price_levels(neckline, bottom, atr, cfg)` 纯函数**，三处价位公式合一（trailing 已有 `compute_stop_price` 单源，base_stop 尚未）；③ DATA_LAKE_PATH 半真配置——明确「仅 default 湖可 env 覆盖」或统一前缀化。
- **验证**：单测断言六层默认值一致；`test_price_levels_single_source`；价位公式 diff 零差异。
- **对抗推演**：抽函数后若参数对象 schema 漂移 → Pydantic model 强约束 + 测试。

---

## §5 时间戳 P2-ts

- **基准**：`discovery/store.py:54`(utcnow)、`backtest/tasks_db.py:37`(now)、`trading/clock.py:30`(now)、`compute_unit/runner.py:140`、`broadcast/__main__.py:251`(自定义 UTC 串)
- **治疗**：统一 Asia/Shanghai aware（带 tzinfo）；**按库逐一标注现有 naive 真实时区分别迁移**——discovery=UTC、trading=本地、compute_unit=UTC、broadcast=自定义串，**不能一刀切按本地**（否则 discovery 的 UTC 数据错移 8 小时）。
- **验证**：跨库时间 join 不再静默差 8h；`test_timestamp_unification`。
- **决策门**：DG-G6 ⚠️ 定稿（**分库迁移，非一刀切**——关键修正）。

---

## §6 决策门定稿（6 道）

| 门 | 裁决 |
|---|---|
| DG-G1 | ✅ 先本机 `pip install -r requirements.txt` 跑绿 run_checks（N1 前置），再改 ci.yml |
| DG-G2 | ⚠️ **cookie 优先**（同源自动、不进 URL）；query token 仅内网 fallback + access log 脱敏 |
| DG-G3 | ✅ fail-closed：模拟盘=停手+告警，实盘=`_CriticalHalt`；基线修复同体落地 |
| DG-G4 | ✅ 200日均线+宽度双确认，空头停手，inner 改各年 min calmar；**红线：regime 阈值不进 TPE** |
| DG-G5 | ✅ 0.25× 起步，上限 0.5× 需样本外验证 |
| DG-G6 | ⚠️ Asia/Shanghai aware，**分库标注 naive 真实时区分别迁移** |

---

## §7 执行顺序与分支隔离

1. **G 波**（G1-G7）→ 分支 `fix/p0-guards`，与 `opt/p1-vectorization` 物理隔离。G1（CI）先复活保护链，G2-G7 随后。
2. **A 波**（A1-A5）→ 待 P1/P5 节奏明确后切入；A2 并入轨 B P5。
3. **C1 / P2-ts** → 可与 G 波并行（低耦合）。
4. 每工单独立 commit + 独立验收门，可暂停可 revert。

---

## §8 文档订正同步项（spec 批准后随工单改，避免现在散改）

- **tech-debt 补 2 条**（N5）：① trailing 动态收紧删除（engine.py:433，live P0 待重做）② run_data_check.py:18/91 data→trading 反向依赖。
- **闭 1 项**（N4）：gateway_service 日志旧名（trading_service）stale 注释——已由 `96d55418`(08-12) 修复。
- **订正 1 处**（§1.3）：account_daily「已闭环」→「08-11 治静默 + fail-open 残留 + G3 收尾」。
- **P2-W2 顺带清理**（N2/N3）：io/orders.py:29 confirm 残参 + compute/__init__.py:43 出场侧第 4 硬耦合 + 剔除 plan.py:33 误列。
