---
title: 全项目审计整改设计——保护链复活（G）+ 策略可信门（A）+ 架构债对齐（P2）
date: 2026-08-13
revision: v1
status: draft（待用户审阅）
author: audit-remediation-session（peer reviewer / risk officer 姿态）
scope: 2026-08-13 全项目审计（11 路并行只读 + 亲验）复核后的整改：P0 止血 G1-G7（CI/鉴权/熔断/台账/超时/原子写/竞态硬化）+ P1 策略可上实盘前提 A1-A5（市场状态闸/walk-forward/成交真实性/仓位护栏/价位单源）+ 配置收口 C1 + P2 架构债对齐（含时间戳新治理点）。审计报告本身 4 处事实修正一并收录，防后人引用错报告。
related:
  - docs/superpowers/specs/2026-08-11-tech-debt-governance-master-design.md（架构债总纲·本 spec 与其对齐）
  - docs/superpowers/specs/2026-08-12-overall-optimization-design.md（优化计划 P0-P6·A2 与 P5 衔接）
  - docs/architecture/06-tech-debt.md（债务切片·单一归宿）
  - docs/guardrails.md（CI 护栏·G1 修复依据）
---

# 全项目审计整改设计

> 本 spec 的输入 = 2026-08-13 全项目深度审计报告 + 当日 6 路并行复核（HEAD `47561049`）。
> 与架构债总纲同样的规矩：本 spec **不改代码**，给出统一裁决 + 依赖编排 + file 级接缝 + 决策门 + 验证手段；
> 实施由后续计划（§8 工单映射）落地，实施前按总纲 §11「现状重验纪律」re-verify 行号。
> 本 spec 行号基准 = **2026-08-13（HEAD 47561049）**。

> ⚠ **先读 §0.1**：审计报告整体可信，但 4 处实质偏差须带着走（P0-3 机制不实、前端"三屏全废"夸大、engine re-export 占比虚高、tushare/yfinance 挂起并非无界）。本 spec 已按修正后事实立项。

---

## 0. 审计复核结论（先给裁决，防引用错报告）

### 0.1 报告准确性裁决（6 路并行复核 · 2026-08-13）

| 审计 P0 | 复核裁决 | 修正后事实 |
|---|---|---|
| P0-1 CI 保护链失效 | ✅ **属实** | `ci.yml:43/51/54` 三处断点：07-12 引入时路径正确，07-25（scripts→ops）与 07-26（web→presentation/web）两次迁移后未同步。docs/guardrails.md:93 与 `ops/run_checks.py` 自身路径全对，唯独 CI yaml 错 |
| P0-2 熔断 fail-open + 基线漏采 | ✅ **属实（附注）** | `breaker.py:50-51` 基线缺失 return False 属实；grep 实证无第二道组合级回撤闸。**注**：08-11 已加"基线告警 + T-1 close 兜底"（`ae57b0e4`/`5eee9302`，总纲 §8 记"已闭环"）——治的是"静默"，fail-open 语义残留，本 spec G3 收尾 |
| P0-3 pre_open 双挂单竞态 | ⚠️ **机制不实，降 P1** | `has_order(:439)→insert(:457)` 之间**无 await 让出点**，单事件循环下双挂单不可能实际发生。真实残余：双入口无锁 + `insert_order` 返回值被忽略（DB 幂等拦不住继续 `_submit`）+ `begin_run` 非原子 → G6+G4 硬化 |
| P0-4 鉴权裸奔 + 0.0.0.0 + SSE 无鉴权 | ✅ **属实** | `auth.py:58-65` fail-open（docstring 明示设计语义非缺陷）；token 不在 .env.example/.env；start_server.bat 不设；`trading/__main__.py:371` host 0.0.0.0；`logs.py:164-167` SSE 无鉴权扇出 root logger（main.py:360-383） |
| P0-5 外部 SDK 无超时 | ⚠️ **部分属实** | 真无界仅两处：FRED（fredapi 0.5.2 SDK 本身无 timeout 能力）+ QMT `get_full_tick`（qmt_quote.py:148 无 wait_for，阻塞事件循环）。tushare/yfinance 调用处无显式 timeout 属实，但 SDK 默认 30s/10s 有界、异常会抛、熔断链可触发 |
| P0-6 数据非原子写 + DROP 重建 | ✅ **属实** | `sync_daily_incremental.py:258-259` 直写无 tmp+fsync；`state_store.py:173/208` DROP 重建（docstring 明示 live 前可接受）；`job_ledger.py:57-63` 无 WAL/timeout（四库中唯一） |

**其余抽查**：模块设计/配置漂移（stop_atr_mult 2.0 残留 plan.py:98、trailing 5/0.1 vs 0/0.0、tp1_portion 0.0 vs 0.5）/时间戳三口径/fill 半迁移/出场侧硬耦合（3 处实锤）全部属实。**统计数字全部经 `logs/neckline_fullscan_trades.csv` 重算对上**（16699 笔、win 38.8%、EV +0.372%、中位 −2.9%、2018/2022 分年负期望精确；payoff/Kelly 差 ≤0.1pp 属舍入口径）——颈线法可信度结论成立，A 系列立项有据。

**报告夸大处**（引用时必须修正）：
1. P0-3"真金双挂单"——当前代码不可能发生（见上表）；
2. "前端研究动线全断/三屏全废"——8 路由 7 视图全在（`presentation/web/src/router/index.ts:37-47`），真实问题仅后端 `/api/v1/caisen/*` 下线而 `web/src/api/caisen.ts` 仍调用；
3. "engine 约一半 re-export"——实测 8 处 noqa、约 196 行 ≈ 12%（5 符号双零引用属实）；
4. 口径类："37 函数"仅公开口径（实际 41 def）、"29 文件扇入"无法复现（严格 import 实测 55）、"1630 测试"实测收集 1659（14 文件收集失败，见 G1 前置）。

### 0.2 复核新发现（报告未覆盖，本 spec 收编）

| # | 事实 | 归处 |
|---|---|---|
| N1 | 本机 venv 缺 pyarrow/optuna/yfinance → **14 个测试文件收集失败**（requirements.txt 已含三者，CI 装齐后不受影响） | G1 前置 |
| N2 | `trading/io/orders.py:29` 仍带 `confirm: bool = True` 且 :44 向已无 confirm 的 gateway_service 透传 → 若被调用即 TypeError（当前零消费者） | P2-W2 顺带 |
| N3 | `compute/__init__.py:43` 出场符号 re-export 未列入审计清单 | P2-W2 顺带 |
| N4 | gateway_service 日志旧名已被 `96d55418`（08-12）修复 | 文档订正（闭项） |
| N5 | tech-debt 缺 trailing 删除、run_data_check 反查条目 | §5 文档订正 |

---

## 1. 范围与对齐声明（与既有三条轨道的交叉映射）

> 本 spec 治理的是**保护链失效 + 策略可信**；架构债总纲治**模块拓扑**；优化计划治**回测口径/性能**。三者正交，交叉点显式对齐，不重复立项。

| 本 spec 治理点 | 内容 | 与既有治理的关系 |
|---|---|---|
| G1 CI 复活 | 保护链本身 | **新** |
| G2 鉴权 fail-closed | 默认部署安全 | **新** |
| G3 熔断 fail-closed | 风控语义收尾 | 总纲 §8 记 account_daily"已闭环"——**订正**：08-11 治"静默"，本项收 fail-open 语义（06-tech-debt.md:65 仍 Critical，见 §5 文档订正） |
| G4 job_ledger 原子化 | 实盘协调点防护 | **新** |
| G5 SDK 超时注入 | 韧性链缺口 | 与总纲 M3 连接韧性（T9 探针）互补：M3 治**主动探针**，G5 治**被动挂起** |
| G6 pre_open 双入口硬化 | 审计 P0-3 降级 | **新** |
| G7 原子写 + DROP→RENAME | 数据原子性 | T13-A write-side 守卫（W0 已治 3 处裸写）同族——G7 补"原子性"维度，不重复行数校验 |
| A1 市场状态闸 | 熊市停手 | 06-tech-debt.md:97 明示算法缺口独立跟踪——本 spec 正式立项 |
| A2 跨熊市 walk-forward | 选优去偏 | 优化计划 **P5**（折 2020-21/22-23/24/25/2026 OOS，**已含熊市 2022**）——合并推进 + 本 spec 补"各年 min calmar"决策门（DG-G4 姊妹） |
| A3 回测成交真实性 | 净值高估 | 优化计划 P0-1/P0-2 同族（跳空已修 `min(stop, open)`）——补成交量校验 + 封板流动性 + max_holding 日历日口径 |
| A4 分数 Kelly + 集中度 + 组合回撤 | 仓位护栏 | **新** |
| A5 价位公式单源 + trailing 收口 | 漂移消除 | **新**（小） |
| C1 配置漂移收口 | 六层默认值硬编码 | **新**（含漂移断言测试） |
| P2-ts 时间戳统一 | SSoT 漏洞 | **新治理点**（总纲未覆盖，审计列最大系统性漏洞） |
| P2 其余（state_store 拆分/出场侧 registry/data_service 迁移/垫片清理/前端 caisen/run_data_check） | 架构债 | **映射既有工单**（见 §5），不重复立项 |

**物理隔离声明**：当前 checkout 分支 `opt/p1-vectorization`（优化轨道，`discovery/runner.py` 有未提交改动）。G/A 系列触及 trading/data/strategies，建议独立分支（`fix/p0-guards` / `feat/strategy-trust`）推进，与 opt 轨道零撞文件。

---

## 2. 治理点清单（总表）

| ID | 严重度 | 治理点 | 工单 id | 波次 | 决策门 |
|---|---|---|---|---|---|
| G1 | P0 | CI 三路径修复 + 本机环境补装 | g1-ci-revival | G 波（本周） | DG-G1 |
| G2 | P0 | 鉴权 fail-closed + host 127.0.0.1 + SSE 鉴权 | g2-auth-fail-closed | G 波 | DG-G2 |
| G3 | P0 | 熔断基线缺失 fail-closed | g3-breaker-fail-closed | G 波 | DG-G3 |
| G4 | P0 | job_ledger WAL + BEGIN IMMEDIATE 领取 | g4-ledger-atomic | G 波 | — |
| G5 | P0 | FRED/QMT 超时注入 + tushare/yfinance 显式化 | g5-sdk-timeout | G 波 | — |
| G6 | P1 | pre_open 双入口锁 + insert 返回值检查 | g6-preopen-lock | G 波 | — |
| G7 | P1 | 湖主写 tmp+fsync+rename + 迁移 DROP→RENAME | g7-atomic-write | G 波 | — |
| A1 | P1 | 市场状态闸产品化 | a1-regime-gate | A 波（2-4 周） | DG-G4 |
| A2 | P1 | 跨熊市 walk-forward（并 P5）+ 各年 min calmar | a2-walkforward-bear | A 波 | DG-G4 |
| A3 | P1 | 成交真实性（成交量/封板/停牌日历日） | a3-execution-realism | A 波 | — |
| A4 | P1 | 分数 Kelly + 行业集中度 + 组合回撤闸 | a4-position-guards | A 波 | DG-G5 |
| A5 | P1 | compute_price_levels 单源 + trailing 默认收口 | a5-price-levels | A 波 | — |
| C1 | P1 | 配置六层默认值 SSoT + 漂移测试 | c1-config-sso | A 波 | — |
| P2-ts | P2 | 时间戳时区口径统一 | p2-timezone-ssot | P2（随总纲 W2/W3） | DG-G6 |

---

## 3. G 波：P0 保护链复活（止血）

### G1 CI 复活（最高 ROI，5 分钟修复 + 1 个前置）

**现状**：`ci.yml:43` `cache-dependency-path: web/package-lock.json`（实际 `presentation/web/`）；`:51` `npm ci --prefix web`；`:54` `python scripts/run_checks.py`（实际 `ops/run_checks.py`）。三处任一即挂，CI 自 07-25 起从未真跑 4 项 gate。

**治疗**：三处路径改 `presentation/web/package-lock.json` / `--prefix presentation/web` / `python ops/run_checks.py`（与 guardrails.md:93 及 `ops/run_checks.py` 自身对齐）。

**前置（N1）**：本机 `.venv310` 缺 pyarrow/optuna/yfinance → 14 个测试文件收集失败（`tests/test_integrity.py`/`test_repair_gaps.py`/`test_freshness.py` 等），本机跑 `run_checks` gate③ 现在就是红的。先 `pip install -r requirements.txt` 补装 + 本地 `python ops/run_checks.py` 跑绿，**再改 ci.yml**——否则"CI 复活即红"，护栏首跑即失信（DG-G1）。

**验证**：本地 run_checks 5 项 gate 全 PASS；push 后 CI 绿；收集失败文件数归零。

**对抗推演**：CI 复活后首次跑可能暴露"既有红"（3 周无人守门累积）→ 属预期收益而非回归，逐项修到绿，不降级 gate。

### G2 鉴权 fail-closed + 监听面收窄

**现状**：`presentation/server/http/auth.py:58-65` token 未配 → return None 放行（docstring 自称"开发态放行"）；`QUANTER_API_TOKEN` 不在 .env.example/.env；`scripts/start_server.bat` 只设 `QUANTER_REQUIRE_LIVE=1` 不设 token；`trading/__main__.py:369-371` host 默认 0.0.0.0；`api/v1/logs.py:164-167` `/logs/stream` 无任何 Depends，扇出 root logger 全量日志（main.py:360-383），而 main.py:650-662 其余 6 个 router 全挂 `require_write`。

**治疗**：
1. `require_write` 按模式分层：`QUANTER_REQUIRE_LIVE=1`（start_server.bat 已设）且 token 未配 → 写操作拒绝 + 启动 CRITICAL 告警；开发态保留 fail-open + WARNING（现有语义）。
2. `.env.example` 补 `QUANTER_API_TOKEN` 占位 + 注释；start_server.bat 校验（live 下缺 token 拒绝启动或醒目提示）。
3. host 默认改 `127.0.0.1`（`trading/__main__.py:371` + `http/config.py:45`）；外网访问显式 `SERVER_HOST=0.0.0.0` 才放开。
4. `/logs/stream` 挂鉴权依赖（与其余 router 对齐）。

**对抗推演**：SSE 是 `EventSource`——**不能带自定义 header**，前端 log viewer 接 token 需 query 参数或改 fetch streaming（DG-G2 拍板）。token 加 query 有日志泄漏风险（URL 进访问日志）→ 推荐 token 仅限 live 写操作 + SSE 降级为"仅 localhost 可读"或短时效查询 token。

**验证**：单测——live 无 token → 写路由 401；SSE 无 token 拒绝；.env.example 含 token 占位；host 默认值断言。

### G3 熔断基线缺失 fail-closed（DG-G3）

**现状**：`trading/compute/breaker.py:50-51` `if start_equity <= 0: return False`（docstring 明言"由其他维度兜底"，但 grep 实证**无其他组合级回撤闸**）；`post_close.py:276-297` 已有 T-1 close 兜底 + 无基线时显式 `breaker_skipped=True`（08-11 治的"静默"部分）；`06-tech-debt.md:65` 仍记 Critical。

**治疗**：基线链全部缺失（start_equity<=0 且 T-1 兜底也取不到）→ 不再 return False，改为**触发保护**：模拟盘触发 C-1 熔断（当日停手）+ 告警；实盘 `_CriticalHalt`（动作选择 = DG-G3）。有基线路径行为不变（行为等价红线）。

**对齐订正**：总纲 §8"已闭环"指告警+兜底，本项是 fail-open 语义收尾——实施时同步订正 06-tech-debt.md:65 表述（见 §5）。

**验证**：单测——start_equity=0 → 触发（非 False）；有基线 → 判定不变；无基线路径告警调用断言。

### G4 job_ledger WAL + 原子领取

**现状**：`trading/job_ledger.py:57-63` `sqlite3.connect(db)` 无 timeout/WAL/busy_timeout（四库中唯一）；`:77-82` `begin_run` 用 `INSERT OR REPLACE` 非事务领取（并发同键互相覆盖无仲裁）。

**治疗**：`_connect` 加 `timeout=30` + `PRAGMA journal_mode=WAL`（对齐 `backtest/tasks_db.py:52-54`、`discovery/store.py:68-70`）；`begin_run` 改 `BEGIN IMMEDIATE → SELECT → INSERT → COMMIT` 原子领取（范式照抄 `tasks_db.py:155-175` claim_next_pending）。

**验证**：单测——并发同键 begin_run 仅一个领取成功；全量绿。

### G5 SDK 超时注入（按修正后事实收窄）

**现状（修正后）**：真无界两处——
1. FRED：`data/fetcher.py:438-439` 构造 `Fred(api_key=...)` + `:614-618` `get_series(...)` 无 timeout；fredapi 0.5.2 本身无 timeout 能力（`urlopen` 裸调，挂起无上限）；
2. QMT：`broker/qmt_quote.py:148-150` `await loop.run_in_executor(None, lambda: xtdata.get_full_tick(...))` 无 `asyncio.wait_for`——挂起时 future 永不完成，**阻塞事件循环**（盘中止损巡检每 5min 调 `get_quotes` 路径）。

有界但应显式化两处：`tushare_sync.py:123`、`data/calendar.py:44-47`（SDK 默认 30s）、`yfinance_client.py:79`（SDK 默认 10s）。

**治疗**：FRED → 线程池 + `future.result(timeout=30)`（复刻 `akshare_client.py:40-48` 模式，SDK 无 timeout 只能包裹）；QMT → `asyncio.wait_for(..., 30)`；tushare/yfinance 显式注入 `timeout=30/10`（与 SDK 默认对齐并显式化，防 SDK 版本变更漂移 + 熔断链时序可控）。

**验证**：单测——mock 永不返回的 SDK → 超时异常抛出 → breaker `record_failure` 计数（韧性链恢复触发）。

### G6 pre_open 双入口硬化（审计 P0-3 降级后）

**现状（修正后）**：`pre_open.py:439(has_order)→457(insert)→464(_submit)` 同步段无 await，单事件循环下双挂单**不可能**；但：① `engine.py:1383-1390` cron wrapper 无台账守卫 + `catchup.py:130/156` 双入口无 asyncio.Lock；② `pre_open.py:457-459` `insert_order` 返回值被忽略——DB 写失败（UNIQUE/IntegrityError→False）仍继续 `_submit` 向柜台下真单。

**治疗**：① pre_open 入口加 `asyncio.Lock`（含 catchup 路径，防未来引入 await 或 `--workers>1` 重开窗口）；② `insert_order` 返回 False → 中止 `_submit` + 告警（修现实隐患：DB 幂等拦不住柜台）。

**对抗推演**：加锁是防御性（非修现实 bug）；返回值检查才是本项真实收益。两者都小，一并做。

### G7 原子写 + 迁移 DROP→RENAME

**现状**：`data/tools/sync_daily_incremental.py:258-259` `safe_overwrite`（仅行数校验，`integrity.py:185-190`）+ `to_parquet(LAKE)` 直写目标，无 tmp/fsync/rename——写入中 OOM/断电留半截损坏 parquet，下次 read 需全量回采；`trading/state_store.py:173/208` 旧 schema 迁移 `DROP TABLE` 重建（docstring 明示"live 前可接受"，但连错库/历史回灌即丢成交持仓）。

**治疗**：湖主写改 `tmp 文件 + fsync + os.replace`；迁移 DROP 改 `ALTER TABLE RENAME TO fill_legacy_<日期>`（可回滚、零数据丢失）。

**对齐**：T13-A write-side 守卫（行数骤降）已治 3 处裸写（W0），本项补"原子性"维度，不重复。

**验证**：单测——写入中断（mock）不损坏原文件；迁移后旧表以 legacy 名存在且数据完整。

---

## 4. A 波：P1 策略可上实盘前提

> 底线背景（审计 §二，已重算属实）：全样本 win 38.8%、Kelly≈4.1%、EV +0.372%/笔、中位 −2.9%；2018 win 20.0% avg −2.45%、2022 win 23.9% avg −2.21%；剔除熊市年 Kelly 跃至 9.15%。无前视偏差是优点，但**无前视 ≠ 有 edge**——A 波是"能不能上实盘"的分水岭。

### A1 市场状态闸产品化（DG-G4）

**现状**：`backtest/tools/market_regime_filter.py` 是独立诊断脚本（顶层 `pd.read_csv` 即执行），生产路径 grep 零引用（仅 2 处 .md 提及）；策略在熊市系统性亏损时无任何停手机制（"四层动能熊市过滤已证伪被移除"）。

**治疗**：诊断脚本 → 生产模块（判据选择 = DG-G4）：扫描/下单前置市场状态判定，空头环境停手或降仓；用 2018/2022 分年数据回测验证闸的有效性（开闸期间策略负期望是否被截断）。

**对抗推演**：市场状态闸自身也是"在历史熊市调参"的过拟合源 → 判据必须极简（1-2 个宽阈指标），并在 2026 样本外观察其空头判定次数（应为 0 或极少，否则误伤当前牛市）。

### A2 跨熊市 walk-forward（并优化 P5）+ 各年 min calmar

**现状**：`discovery/split.py:42-45` inner=2025/outer=2026（熊市 2018/2022 全在窗外）；`search.py:9/53/139` TPE 目标 = inner calmar——选优与去偏都不含熊市，冠军从未在熊市被评估。

**治疗**：与优化 P5 合并推进（P0-2 湖深已实证：各折创板科创 1065-2017 只/年，退市 38 只须保留挂牌期数据）；新增决策门（DG-G4 姊妹）：**inner 目标改"各年 min calmar"**——单年 2025 的 calmar 不再能代表，冠军必须每年（含 2018/2022）都不崩。

### A3 回测成交真实性

**现状**：`strategies/neckline/backtest.py:128 simulate_exit` 成交判定 `low_i <= buy_limit` 即成交、成交价 `min(buy_limit, open)`——无挂单量 vs 成交量校验；`max_holding` 按 bar 计（`:189`，EXEC_DEFAULTS 15 bar，停牌让持有期横跨极长日历日）；跌停封板无流动性校验（止损价已由 P0-1 修为 `min(stop, open)`，但 open=跌停价时仍全量成交）。

**治疗**：成交量维度（挂单量 vs 当日成交额上限校验）+ 封板不成交语义（跌停开盘=止损卖不出，持仓顺延）+ max_holding 改日历日口径。三者叠加让回测净值贴近实盘，Kelly 估计同步下调（预期）。

**对抗推演**：成交量数据质量（单位/含不含盘后）需先小样验证，数据不可靠时降级为"成交额占比粗校验"，不引入假精度。

### A4 分数 Kelly + 集中度 + 组合回撤（DG-G5）

**现状**：`backtest/models.py:40` pos_cap=0.05 ≈ 全样本 Kelly 4.1%——等于按被熊市稀释后的薄边缘满仓下注；`PositionModel`（models.py:39-43）只有 max_positions=6 计数上限，无行业/主题/相关性维度——颈线信号同板块集群出现时 6 仓≈单一行业 30% 敞口；组合级回撤保护仅 -3% 单日一闸（G3 治其失效态）。

**治疗**：① pos_cap 改分数 Kelly（0.25× 起步 = 1%，DG-G5）；② PositionModel 加行业/主题集中度上限（同板块持仓数/资金占比封顶）；③ 组合级第二回撤闸（滚动回撤 X% → 停策略 N 天，A1 市场状态闸的姊妹）。

### A5 价位公式单源 + trailing 收口

**现状**：颈线/tp2/止损价位公式三份复制（审计列项）；trailing grace/step 默认值漂移（critical.py:229-230 env 5/0.1 vs schema.py:52-53/engine.py:1486-1487 四层 0/0.0——消费方已删但 env 是埋好的 live-backtest 漂移种子）。

**治疗**：抽 `compute_price_levels` 纯函数单源（三处调用改引）；trailing env 默认收口到 0/0.0（对齐 schema/engine），或显式声明 trailing 全链路退役。

### C1 配置漂移收口

**现状**：stop_atr_mult `compute/plan.py:98` 残留 2.0 兜底（其余五层已 1.0）；trailing grace/step、tp1_portion（plan.py:68 0.0 vs 其余 0.5）同类。

**治疗**：六层默认值（env/.get 兜底/EXEC_DEFAULTS/schema/critical/engine）收口单一 SSoT + 漂移断言测试（各层默认值一致性入 CI，防再生）。

---

## 5. P2 架构债对齐（映射既有工单，不重复立项）

| 审计 P2 项 | 映射 |
|---|---|
| state_store 按聚合根拆 + `update_order_state`(:419) 接 OrderStateMachine + `apply_fill_to_position`(:676) 公式去重 | 总纲 W2 M2（m2-key-governance）+ 新增"FSM 校验接线"子项 |
| 出场侧走 registry/protocol（engine.py:261 / stop.py:97 / stop_loss.py:126 / compute/__init__.py:43） | 总纲 W2 适配层（h1-broker-layering/h2-callback-ports）顺带 |
| protocols.py:83 confirm 残参 + **N2** io/orders.py:29 透传 TypeError 隐患 | W2 顺带（零消费者，低风险高整洁） |
| data_service.py(311 行) 迁回 data 域 + macro.py:57 抽 service | 总纲 M1 follow-up 类新工单（p2-layer-fix） |
| run_data_check.py:18/91 反查 | 总纲 §2.4 data-cycle-cut **已立项**（迁 ops/）——引用不重复 |
| 时间戳三口径统一（**P2-ts**） | **新治理点**（总纲未覆盖）：UTC naive / 本地 naive / "UTC"字符串并存（trading/clock.py:30、discovery/store.py:55、data_service.py:66），跨库 join 静默差 8h。口径 = DG-G6 |
| fill strategy 列半迁移（9 列 vs 7 列 + `(r.get("strategy") or "")` 兜底） | M2 相关 |
| engine re-export 垫片清理（**实测 ~12%**，工作量预期下调） | W1-B deferred **已立项** |
| 前端 caisen（补后端 API 或退役视图 + api/caisen.ts） | 06-tech-debt.md Low **已有归处**（T2 适配层顺带/独立清理） |

**文档订正（实施时同步 06-tech-debt.md）**：
1. account_daily 条目（:65）补"08-11 已加告警+T-1 兜底；G3 收 fail-open 语义"；
2. **补记** trailing 删除条目（engine.py:433 自承 live P0 待重做）——现 tech-debt 无此条目；
3. **补记** run_data_check 反查（挂 data-cycle-cut 工单）——现 tech-debt 无此条目；
4. N4 闭项：gateway_service 日志旧名已修（96d55418）。

---

## 6. 决策门清单（我给推荐 + 对抗推演；推进对应工单前需拍板）

| 门 | 决策 | 我的推荐 | 阻塞 |
|---|---|---|---|
| 🚪 DG-G1 | CI 修复顺序 | **先本机补装 + 跑绿 run_checks，再改 ci.yml**——CI 复活即红比不复活更伤护栏公信 | G1 |
| 🚪 DG-G2 | SSE 鉴权方案 | EventSource 无 header → 推荐 **localhost 只读降级 + live 下 token query 参数**（短时效）；不接受"仅靠 127.0.0.1 兜底就跳过鉴权" | G2 |
| 🚪 DG-G3 | 基线缺失保护动作 | 模拟盘 = 触发 C-1 熔断（当日停手）+ 告警；实盘 = `_CriticalHalt`。**不选**"仅告警不动作"（那是现在） | G3 |
| 🚪 DG-G4 | 市场状态判据 + 空头动作 | 判据极简：指数 200 日均线下方 + 市场宽度双确认；空头 = **先停手**（降仓是第二步优化，不做过度设计）。inner 目标同步改"各年 min calmar" | A1/A2 |
| 🚪 DG-G5 | 分数 Kelly 起点 | 0.25×（≈1%/笔）起步，季度复盘上调，上限 0.5× | A4 |
| 🚪 DG-G6 | 时间戳统一口径 | 存储统一 **Asia/Shanghai aware**（交易域唯一真相），展示层再本地化；存量 naive 数据按本地时间解释，迁移脚本一次写清 | P2-ts |

---

## 7. 验证与回归防护

**每工单合并门**（G 波）：新增守卫单测绿 + 全量单测绿 + **行为等价**（保护类改动不得改变"有基线/有 token/有超时"路径的既有行为）+ CI 绿（G1 修复后生效）。
**A 波**：每项改动跑分年回测对比（2018/2022/2025/2026 分年报告 diff）+ A1/A4 需样本外（2026）观察无误伤。
**执行顺序**：G 系列互不撞文件可并行小步（各自独立 commit）；A 系列串行（A1 闸影响 A2 回测口径）；P2 随总纲 W2/W3 波次并轨。
**主要风险**：G2 误伤开发态（中/低）→ 按 QUANTER_REQUIRE_LIVE 分层；G3 误触发停手（中/中）→ 仅基线链全缺失才触发 + 告警先行；A1 状态闸过拟合（中/中）→ 判据极简 + 2026 OOS 观察；G7 原子写引入半截文件窗口（低/低）→ tmp 同目录 rename 原子。
**回滚总则**：每工单独立 commit，小步提交；撞 🚪 决策门即停，不擅自越门。

---

## 8. 工单拆解映射（spec 批准后由 writing-plans 生成）

| 波次 | 治理点 | 实施计划（待生成） | 决策门 |
|---|---|---|---|
| G | G1 | g1-ci-revival | DG-G1 |
| G | G2 | g2-auth-fail-closed | DG-G2 |
| G | G3 | g3-breaker-fail-closed | DG-G3 |
| G | G4 | g4-ledger-atomic | — |
| G | G5 | g5-sdk-timeout | — |
| G | G6 | g6-preopen-lock | — |
| G | G7 | g7-atomic-write | — |
| A | A1 | a1-regime-gate | DG-G4 |
| A | A2 | a2-walkforward-bear | DG-G4 |
| A | A3 | a3-execution-realism | — |
| A | A4 | a4-position-guards | DG-G5 |
| A | A5 | a5-price-levels | — |
| A | C1 | c1-config-sso | — |
| P2 | P2-ts | p2-timezone-ssot | DG-G6 |

---

## 9. 附录：勘察事实索引（file:line · 2026-08-13 基准）

> 行号基准 = 2026-08-13（HEAD `47561049`）。实施前按总纲 §11 纪律 re-verify。

### G 波
- ci.yml:43/51/54（三断点）· guardrails.md:93（文档正确）· `ops/run_checks.py`（5 项 gate，路径全对）
- auth.py:41-42（`_configured_token`）· :58-65（fail-open）· scripts/start_server.bat（无 token）· trading/__main__.py:369-371（host 0.0.0.0）· http/config.py:45 · api/v1/logs.py:164-167（SSE 无鉴权）· main.py:360-383（root logger 扇出）/644（include）/650-662（其余 router 挂 require_write）
- compute/breaker.py:23-53（check_daily_loss_limit，:50-51 fail-open）· post_close.py:276-297（T-1 兜底 + breaker_skipped）/314-315（调用）
- job_ledger.py:57-63（_connect 无 WAL/timeout）/77-82（INSERT OR REPLACE）· tasks_db.py:52-54（WAL+timeout=30）/155-175（BEGIN IMMEDIATE 范式）· discovery/store.py:68-70
- data/fetcher.py:438-439（Fred 构造）/614-618（get_series）· broker/qmt_quote.py:148-150（run_in_executor 无 wait_for）· tushare_sync.py:123 · yfinance_client.py:79 · data/calendar.py:44-47 · akshare_client.py:40-48（30s 线程池范式）· alpha_vantage_client.py:57（httpx 15s）
- pre_open.py:439（has_order）/457（insert）/464（_submit）· engine.py:1383-1390（cron wrapper）· catchup.py:130/156
- sync_daily_incremental.py:258-259（直写）· data/integrity.py:185-190（行数校验）· state_store.py:170-173/203-208（DROP）

### A 波
- discovery/split.py:42-45（inner=2025/outer=2026）· search.py:9/53/139（TPE 目标 inner calmar）· runner.py:174
- backtest/tools/market_regime_filter.py（零生产引用）· strategies/neckline/backtest.py:128（simulate_exit）/175（buy 成交）/189（max_holding bar）/253-258（min(stop, open)）
- backtest/models.py:26/39-43（PositionModel，pos_cap 0.05，max_positions 6）· trading/compute/risk.py:70-83（仅 3 闸）
- critical.py:205（stop_atr_mult env 1.0）/229-230（trailing 5/0.1）/214（tp1_portion 0.5）· compute/plan.py:98（2.0 残留）/68/104（tp1 0.0）· strategies/neckline/schema.py:34/52-53

### P2
- state_store.py:939/960（状态集）/419（裸写 state）/676（公式复制）· engine.py:261/stop.py:97/stop_loss.py:126/compute/__init__.py:43（出场侧硬 import）· protocols.py:78-84（confirm）/gateway_service.py:589（无 confirm）/io/orders.py:29,44（残留透传）· data/tools/run_data_check.py:18/91（反查）· presentation/server/services/data_service.py（311 行）· api/v1/macro.py:57 · engine.py:433（trailing 自承删除）· trading/clock.py:30 / discovery/store.py:55 / data_service.py:66（时间戳三口径）· config/data.py:39（DATA_LAKE_PATH 仅 1 消费）+ registry.py（41 处硬编码）

### 复核裁决速查
- 审计统计数字：`logs/neckline_fullscan_trades.csv`（16699 行）重算全对（payoff/Kelly 差 ≤0.1pp 舍入口径）
- 测试：pytest.ini:16 默认 deselect e2e_long；收集 1659（14 文件因本机缺 pyarrow/optuna/yfinance 收集失败）；tech-debt 记 08-12 全量 1687 绿
- 前端：router/index.ts:37-47 全 8 路由注册；web/src/api/caisen.ts 调已下线的 /api/v1/caisen/*
