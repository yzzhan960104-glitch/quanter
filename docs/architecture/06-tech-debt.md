> 最近复核：2026-08-16（debt/new-debt-0816 波次销账对账）· 维护者：new-debt-wave session ·
> 权威归宿：**技术债 / 痛点 / god module 判定**（单一归宿）。模块结构（不判债）见 [#2](02-module-dependencies.md)；本视图是 #2 的「债务切片」——只画债-bearing 项 + 严重度，不重复依赖边。
> **波次记录**：`debt/full-wave-0815`（T1-T18 · 2026-08-15）——对 2026-08-14 批判性复核（[deep-dives](deep-dives/2026-08-14-critical-review.md)）登记的 CR-1..11 全量清偿 + W1-B/W2/TD/SS/CN 遗留收口。**`debt/new-debt-0816`（N1-N6 · 2026-08-16）**——对上一波登记的全部新债（High 停牌真值 / Medium entry_date + 死种子 / 测试卫生 pytest spawn / Low 批）清偿 + **根因叙事勘误**（000029.SZ 铁证不成立，见 High 表）。清偿后记见 [deep-dives 文头「2026-08-15 清偿后记」](deep-dives/2026-08-14-critical-review.md)。

# #6 技术债 / 已知缺口分布

路线图即技术债热力图。严重度四级（Critical / High / Medium / Low），每项链对应 wayfinder 工单（→ 治理归宿）。

## 债务热力图（2026-08-16 新债清偿终态）

```mermaid
flowchart LR
  subgraph SEV["严重度"]
    C["Critical"]:::crit
    H["High"]:::high
    M["Medium"]:::med
    L["Low"]:::low
  end

  subgraph DONE["✅ 2026-08-15 全量销账（debt/full-wave-0815）"]
    E["✅ engine.py god module<br/>T1 (08-10) 3437→1546<br/>+ W1-B re-export 删除 T10<br/>现 1481 行 · 零中转站"]:::done
    AS["✅ 风控链 fail-closed 全收口<br/>G3 基线 8d4ef714<br/>CR-4 curr 缺失 7296e3f3<br/>CR-3 盘中前移 0072e1a1"]:::done
    PL["✅ CR-2 价位单源<br/>strategies/neckline/price_levels.py<br/>fbbeb82a · golden 钉死"]:::done
    DVP["✅ CR-1 discovery 死页<br/>5d999375 · 形状守卫入 gate②"]:::done
    Q["✅ broker 四文件分层<br/>W2-H1 e3195df0<br/>qmt.py 1692→132 + BrokerProtocol"]:::done
    TB["✅ 回调体 Ports 化<br/>W2-H2 7be0419f<br/>order_state 副作用显式注入"]:::done
    OSS["✅ CR-5 漏挂观测三件套<br/>6d434ce4 (+T17 集合补全)<br/>反向扫描+孤儿口径+fill CHECK"]:::done
    OBS["✅ CR-7 巡检调度+双通道<br/>42e3bd96<br/>QuanterAudit DAILY 16:05"]:::done
    TD["✅ TD data→trading 边清零<br/>T9 ddb9c9a9<br/>#2 复跑 = 0"]:::done
    SS["✅ T6 SSoT 四决策定稿<br/>fill CHECK/is_vetoed/actual_sid<br/>StopLossContext 全落"]:::done
    CN["✅ 连接韧性 T9/T10/T11<br/>探针 299ab2de + 嵌套去重 710d9c30<br/>connect 留痕 + 韧性收尾 bf55c6ae"]:::done
    GOV["✅ CR-9 工单回填<br/>e6100a79 · 8 工单闭 + MAP 重写<br/>frontier 仅余 T3"]:::done
    CIG["✅ CR-10 CI 心跳元守卫<br/>6f70faa6<br/>ci-heartbeat.yml + 周一 schedule"]:::done
    DD["✅ CR-11 文档漂移<br/>guardrails/data-source-of-truth<br/>6f70faa6 + bc69d546 刷新"]:::done
    DOC["✅ 过时文档丙删<br/>早已执行（Phase 0 收尾）"]:::done
  end

  subgraph NEW["✅ 2026-08-16 新债清偿（debt/new-debt-0816）"]
    SUS["✅【High】scan 停牌真值缺口<br/>N1 日级判定+共识启发式+sidecar<br/>N1b 探针分类 unjustified 16371→0<br/>根因叙事勘误：000029 铁证不成立"]:::done
    EDT["✅【Medium】entry_date 取写入日<br/>N2 traded_time 四态解析<br/>首 BUY 锁成交日 + 回退保护"]:::done
    SEED["✅【Medium】save_plan_legacy 死种子<br/>N3 tests/_plan_seed.py 单一归宿<br/>三处手写同构归一"]:::done
    LC["🔶【Low】波次遗留清理清单<br/>N5 处置 ②③⑨⑪ + D/E 批<br/>④ N3 + ⑦⑩ stale 勘误<br/>余项 ①⑤⑥⑧⑫-⑮ + N 波 minors follow-up"]:::low
  end

  DI["🔶 data 完整性运行态<br/>unjustified=0（N1b 分类完成）<br/>13,535 段推定不可补 · 湖未变一行<br/>02:00 daemon 首跑待观察"]:::med

  DI -.->|真值层| SUS
  SUS --> RESID["→ 残留：13,535 段 probe_zero_day 推定<br/>920xxx.BJ daily 不载——真补需换数据源<br/>--clear-unfillable --reason 可重置推定"]:::low

  classDef crit fill:#f88,stroke:#c00,color:#400
  classDef high fill:#fc8,stroke:#a60,color:#420
  classDef med fill:#ffd,stroke:#990,color:#440
  classDef low fill:#eef,stroke:#88a,color:#335
  classDef done fill:#cfc,stroke:#090,color:#030
```

## 债务清单（按严重度 · 销账终态）

### Critical（阻塞 live / 阻塞演进主脊柱）

| 项 | 物理事实 | 治理归宿 |
|---|---|---|
| ✅ **engine.py god module —— T1 + W1-B 全收口** | T1（2026-08-10）：3437→1546 行，8 集群外迁，`_ACTIVE_ENGINE` 单例桥清零。**W1-B/T10（`e2a6153f` · 2026-08-15）**：engine re-export 兼容块整体删除（1693→1469 行）+ gateway lazy 顶部化（模块对象风格）；7 处外部引用迁物理真身、~120 测试触点迁移；现 engine.py **1481 行**，纯调度/装配/gate/job wrapper，**零符号中转站**（grep `^from trading.` 仅 15 行自用直 import）。行为等价：T10 全量 1908+2 存量红（基线同款）、T15 L4 双跑零 diff | ✅ done（[T1](../../plans/wayfinder/T1.md) + W1-B） |
| 🔶 **data 完整性（运行态尾巴——代码路径全治，真值层 N1/N1b 分类收口）** | 代码侧全治理：T13-A/B + D1（08-11/12）写入守卫/scan gate/异步补采；**CR-6/T6（`4b636756`+`3abea439` · 2026-08-15）回路复活**——单日异常部分落盘（不再整轮丢弃白拉）+ daily/adj 原子入列（防 NaN 落湖）+ `REPAIR_DAY_SLEEP` 限频降速；**实弹 350/350 日零中断、熔断 sidecar 复位、湖 byte-identical 零漂移**。**N1/N1b（`87e4132d`+`fa4e9c86` · 2026-08-16）真值层收口**：终态 scan `unjustified_gaps=0`（confirmed 226 + probed 13,535 全部 sidecar 排除，总缺口 22,802 段全部停牌合法跳空）——「配额被永不可补段死占」回路解除。🔶 **运行态注记（诚实分级）**：unjustified=0 是「分类完成」**非「补上」**——13,535 段 probe_zero_day 为中点采样**推定**（98.4% 占比），湖未因 classify 变一行（classify 物理无写湖路径）；真补需换数据源（920xxx.BJ 北交所 Tushare daily 不载）。**02:00 daemon 首跑待观察**：15701 完整性闸按 unjustified 口径应放行，需运行态实证 | 🔶 观察中（残留转 Low 留档：推定段 + daemon 首跑） |
| ✅ **【CR-1】discovery 研究首页死页面** | `5d999375`（T1 · 2026-08-15）：`discovery.ts` 四函数剥壳语义直返 + **形状契约静态守卫 `check_no_double_unwrap` 入 gate②**（单行正则、读不到 fail-closed）；vitest 13 文件 33 测全绿 + vue-tsc 0 错；顺手修 predev 死路径（`scripts/ops/check_ports.py`→`ops/`）。守卫局限（多行解构不命中）已知成文 | ✅ done（T1） |
| ✅ **【CR-2】入场价位三件套双份实现** | `fbbeb82a`（T7 · 2026-08-15）：单源真身 `strategies/neckline/price_levels.py`（`PriceLevels` + `compute_price_levels` + `PRICE_LEVEL_DEFAULTS`）——brief 原址 trading/compute/ 违分层铁律（strategies 禁 import trading），改址仓内先例同向（ExitAction 同款）。backtest.py/plan.py/两 diag 副本全收编；C1 兜底 2.0→1.0（实证 2.0 幽灵默认从未生产生效，零行为变化）；11 golden 测试钉死 + backtest/plan 行为级等价。残留：`method_v0.py:328/337` 识别层 rr 门控副本（语义不同，未收编——见 Low 清单）| ✅ done（T7） |
| ✅ **account_daily.start 漏采 → 全链 fail-closed** | G3 `8d4ef714`（08-13）：基线缺失 live `raise _CriticalHalt` 停调度 + pre_open T-1 兜底回填。**CR-4/T2（`7296e3f3`）补对称缺口**：curr_equity 缺失/异常 live 推 CRITICAL + `_CriticalHalt`（原 fail-open 静默 skip）+ 收盘快照失败 live 有声（掏空次日基线链的静默面收口）。测试 `test_breaker_fail_closed.py` 9 测钉死（live halt ×3 + dry skip ×2 + 快照告警）| ✅ done（G3 + T2） |

### High（演进主脊柱缝合点）

| 项 | 物理事实 | 治理归宿 |
|---|---|---|
| ✅ **【2026-08-15 登记 · 08-16 清偿】scan 停牌真值缺口（N1 + N1b）** | **N1（`87e4132d`）**：find_gaps **日级判定**替代段级 `all()`（旧逻辑一长段只要有一天不在 suspend_d 即整段误判 unjustified——放大器）；**长洞市场共识启发式**（段 ≥10 交易日 + 每日湖在场数 ≥ 窗口中位数×0.8 + 分母下限 1000，只对 2017+ 有效）——2,610 段转 suspend_suspected（000670/000792/000995 三铁证全中）；**unfillable sidecar** 双侧跳过（repair 拉空实证标记 market_empty/symbol_absent）+ recency-first 配额防盲区段永占。scan unjustified 16,371 段 → 13,761 子段（消费 key 不变）；实弹 repair 一轮：50 子段全部源零行（0/107 探针命中）——真可补率 ≈ 0，是数据事实非实现缺陷。**N1b（`fa4e9c86`）**：`--classify` 探针分类——中点日分组一日一拉（13,535 子段仅实探 2,036 日）、daily-only 在场判定（**工程发现：adj_factor 对停牌股连续发布，symbol 级任一接口在场即非零会全数漏标**），58min 全量实弹：**unjustified 13,717 → 0**（probed 13,535 / confirmed 226），daemon fail-closed 闸条件达成。**根因叙事勘误（T6 旧报告铁证不成立）**：「000029.SZ 恒大重组 1514 天洞误判 unjustified」经 scan_pre_t1 全量段实证 `suspend_justified=True`——**从未进入 unjustified 集**，公开事实吻合恰证明 suspend_d 覆盖了它；真形态 = **2019-2022 suspend_d 长停牌记录稀疏**（S 行 2018=42,976 → 2019=5,752）+ 段级 all() 放大 + **920xxx.BJ 北交所 Tushare daily 不载**（真缺大头 2016-2022 ~13,093 段）。残留：13,535 段推定不可补（真补需换数据源）+ T1 留档 minors（Low 表） | ✅ done（N1+N1b · 素材 task-1/1b-report） |
| ✅ **broker/qmt.py 业务层堆补丁 —— W2-H1 四文件分层** | `e3195df0`（T13 · 2026-08-15）：`qmt.py` 1692→**132 行**（类组装 + 显式列名 re-export），拆 `qmt_connection.py` 1025（契约根/常量/12 辅助/8 回调/连接）、`qmt_io.py` 334（6 IO 方法）、`qmt_business.py` 479（15 业务方法）；新增 `trading/broker_ports.py` **`BrokerProtocol`**（runtime_checkable 最小契约面 + Mock 负面钉子）。**AST 级 53/53 函数体逐字一致**（逻辑只搬）；常量值零漂移（logger 名锁定 `broker.qmt`）；全量 1933 passed 基线同款 | ✅ done（W2-H1 · T13） |
| ✅ **双向耦合 trading↔broker —— W2-H2 回调 Ports 化** | `7be0419f`（T12 · 2026-08-15）：`handle_order_update(engine,...)` → `handle_order_update(ports,...)`，16 处 `_state_store` 副作用 + 3 处 `engine._gw` 经 `EnginePorts.state_store/gateway` 显式注入；engine 侧薄 wrapper 调用时快照。08-04 幂等红线载体逐行保形。#2 复跑边权 6/6（四文件分层后记账面变化，见 #2 漂移注记）。T13 裁定 `ports.gateway` 保留（回调体查**实例态** `_orders/_seq_to_real`，模块分层替代不了实例锚点） | ✅ done（W2-H2 · T12） |
| ✅ **【CR-3】「日内熔断」实为盘后闸** | `0072e1a1`（T8 · 2026-08-15）：评估点前移——`PortfolioBreakerThrottle`（5min 节流，经 ports 注入）挂进 stop_loss 30s 巡检（⑤撤单后）；三分支：触发→先撤后 `emergency_halt`（**不停调度保监控存活**）/评估失败 streak≥3 才告警/基线缺失→breaker fail-closed SSoT（live 转 emergency_halt 不停调度）。post_close 盘后闸仍完整在岗兜底。5 新测 + tests/trading 615 全绿。⚠️ 语义边界（已入 [guardrails §六](../guardrails.md)「emergency_halt 后的实际状态与人工解锁 SOP」——2026-08-15 终审起该交叉引用真实成立）：lock_down 后 `_gw_health_gate` 每轮 skip monitor（既有引擎行为）——「保监控存活」实为调度器存活 + health_guard 在岗可人工解锁；残余持仓在 lock_down 期间无止损覆盖，人工接管。**首轮评估与 pre_open 基线写入竞态已修（终审 I-1）**：stop_loss 读 start None/≤0 时先走 T-1 close 兜底（post_close 同款 + WARN），两级全失效才 fail-closed——盘后启动首轮假阳性 halt 不再发生（原 follow-up 销账） | ✅ done（T8 + 终审 I-1/I-2 收口） |
| ✅ **【CR-5】「防超卖 > 防漏挂」三层同向盲区** | `6d434ce4`（T3 · 2026-08-15）三件套：① audit 反向扫描（fill 净额≠0 而 position 缺行/为 0 → 漏挂向告警）；② 孤儿口径单源常量（删从未写入的 OPEN，补生产实写集）；③ `fill.direction` CHECK（DDL 两处 + G5 备份回灌迁移带 account_id/strategy 保数据 + `insert_fill` 入口 ValueError 先于 DB CHECK 防误吞）。**T17 补全（T3 遗留）**：孤儿集合再补 DRY_RUN/BLOCKED/REJECTED/DIRECTION_UNKNOWN 四个下单审计实写 action + :91 注释订正（SUBMITTED 实写 order 表）+ 巡检连接 `mode=ro`。风险取向已显式写进 [guardrails §六](../guardrails.md)（T5） | ✅ done（T3 + T17 补全） |
| ✅ **【CR-4】curr_equity 缺失静默跳过** | 见 Critical 表末行（`7296e3f3` · T2）——原 High 条目随对称收口升级销账 | ✅ done（T2） |

### Medium（横向治理）

| 项 | 物理事实 | 治理归宿 |
|---|---|---|
| ✅ **双向耦合 trading↔data（TD）—— 边清零** | `ddb9c9a9`（T9 · 2026-08-15）：检查点入口本质是 ops 编排件 → `data/tools/run_data_check.py` 迁 `ops/`（9 文件 +117/−71，bat/tests/docs 引用全量更新，schtasks 元数据实证无需改）；`expected_latest_trade_day` 下沉 `data/calendar.py` + trading 侧 re-export 兼容。**#2 复跑 `data→trading = 0`**（基线 1）——依赖图最后一条 data→trading 反向边清零 | ✅ done（T9） |
| ✅ **state_store SSoT 演进（SS）—— 四决策定稿** | [T6 工单 closed](../../plans/wayfinder/T6.md)（T16 · 2026-08-15）：① plan→SQLite 不重启（DG-5 划线）② 多策略/多账户隔离=W3 划线 ③ 表扩展=Phase A/B/C 已治 + fill.direction CHECK（T3）④ 读写键对齐=`is_vetoed` 单点（T11 `710d9c30`）+ **actual_sid DB 单源**（M2/T14 `ccaad5c9`：`get_session_id/set_session_id` 列级写口，修 L3 upsert clobber + B2 轮换双写欠账）+ `StopLossContext`（T11） | ✅ done（T6/T11/T14/T16） |
| ✅ **连接韧性（CN）—— T9/T10/T11 全闭** | T9 探针 `299ab2de`（`probe_account_status` 线程池+超时 + health_guard 集成）；**T10 实证关闭**（`710d9c30`）：实测发现真嵌套为 venv launcher 单树形态（1456→11548→spawn worker）→ 补 `_drop_engine_descendants` 递归祖先链去重（孙辈经非匹配中间进程挂下不漏杀）+ 7 用例；**T11** connect rc=-1 每轮 `logger.warning` 留痕（观测补齐零行为变更，G8 重试形态保持）。T10/T11 工单 Resolution 均已闭合 | ✅ done（bf55c6ae 收尾） |
| ✅ **【CR-7】audit_ssot 巡检无人调度 + 告警单通道** | `42e3bd96`（T4 · 2026-08-15）：① `LocalFileChannel` 本地第二通道（append `logs/alerts.log`、永不抛、无条件装配、`__file__` 锚定项目根）② `register_audit()` schtasks **QuanterAudit DAILY 16:05**（已注册实证 Ready；16:05=post_close 后避开 17:00 管道）③ `ops/run_audit.bat`（T17 补 `if not exist logs mkdir logs` 守卫）。清退红线测试锁定（误入 RETIRED/LEGACY 会删成静默裸奔）。smoke 暴露 2 项存量 FAIL（account_daily 闭合 11 条 + 端口无 pid 文件）→ 属实存量，T16 已清 ACC debris / 端口项属运维侧旧链 | ✅ done（T4） |
| ✅ **【CR-8】前端观测面断链族** | T16 `e6100a79` + T17 收官：② `/macro/sector/flow` **已删**（sector 湖 2026-07-27 退役恒空；端点收缩 `/macro/pool` + 前端板块图块删 + 404 防复活钉；顺手修 pool 形状错位 `string[]`→`{symbol}[]`）；③ 孤儿路由显式处置——**training×5 保留**（隐藏消费方 `infra/tools/dingtalk_review_bridge.py` 常驻桥消费 `/training/review`）、**research proposals×5 保留**（同桥 `/research/proposals/review`，:13 注释失准已由 T17 订正）、`/ops/processes` 保留（B2-3 内部观测端点）、`/review/diagnose` 保留（CLI 写面，人工 curl 直调）；① `POST /auth/read-cookie` 前端接入——**N5 补齐（2026-08-16）**：TerminalLogs onMounted 先经 apiClient POST 换 HttpOnly cookie 再开 EventSource（失败吞错直连，dry_run/离线容错），CR-8 至此零残留 | ✅ done（T16/T17 + N5 收尾） |
| ✅ **【CR-9】三套工单体系状态失同步** | `e6100a79`（T16 · 2026-08-15）：wayfinder 8 工单回填（T0.1/T2/T13/T9/T6/T7/T8 闭 + T10/T11 核对已闭）；MAP.md frontier 重写——**剩余 frontier = T3 唯一余项**（W2 主体已落阻塞解除）；Decisions 回填 8 条；sdd G7 补账（progress 条目 + 事后补记报告，复跑 21 绿）+ G8 撞号消歧注。波次收尾三件套（刷 #2/#6/回填工单）即本 T17 | ✅ done（T16/T17） |
| ✅ **【CR-10】CI 曾静默死亡 19 天且无元守卫** | `6f70faa6`（T5 · 2026-08-15）：新建 `.github/workflows/ci-heartbeat.yml`（每日 schedule 断言最近 run ≤7 天，独立 concurrency + 最小权限）+ ci.yml 加周一 schedule 心跳。平台边界成文（GitHub 60 天无活动自动停 schedule）；心跳基线依赖 master 首次成功 run（T18 push 后建立） | ✅ done（T5 · 基线待 T18） |
| ✅ **【测试卫生】（M4 · 08-12）+ silently orphaned patch 审计（T17）+ pytest spawn 卫生（N4）** | M4 已治理（resilience 单例 reset + autouse fixture + canary）。**T17 审计（full-wave）**：临时脚本扫 tests/ 全部 `patch("trading.engine.X")`/`setattr(engine,"X")`——活代码命中 27 符号（字符串 16 + 对象 11）**全部仍是 engine 模块属性，孤儿 = 0**；仅 3 处正则命中位于历史迁移叙述注释内（`_last_quote_blackout_alert_ts`/`trading_plan`/`decide_exit`，均为 W1-A/W1-B 迁移记录，如实保留）。test_pre_open_ledger_semantics 死 import 复核零残留（T11 已清）。**N4/T4（`e2e5c957` · 2026-08-16）pytest spawn 卫生**：`pipeline_then_eod` 的 scan+Popen 块抽模块级 `_scan_and_spawn_repair`（生产语义零变化、返回 unjustified 计数备用）+ 六触发用例整锚 mock——**pytest 不再真实 spawn 生产补采烧 Tushare 配额**（也不再每用例真读 10M 行 parquet；task-4 实证 450s→2.45s）；scan→repair 触发行为归专测用例。审计报告贴波次 commit body | ✅ done（M4 + T17 审计 + N4） |

### Low（清理类）

| 项 | 物理事实 | 治理归宿 |
|---|---|---|
| ✅ **【Medium 严重度 · 08-15 登记 · 08-16 清偿】entry_date 取成交日（N2）** | `076aa44b`（2026-08-16）：`_entry_date_from_traded_time` **四态解析**（≥14 位纯数字取前 8 位 / ISO 与空格分隔取前 10 位附形状校验 / 纯时间与垃圾串 → None）——首 BUY 锁定分支改 `解析值 or clock.today()`：跨午夜盘后补写成交回报不再让建仓日漂一天，解析失败回退写入日（成交落账红线零打断，历史行为保形）。holding_days/超期平仓 pretrade_date/持仓归因消费链同步受益。N5/E 批补 isascii 全角守卫（态③ 全角数字截脏日期钉死）。`scripts/archive/backfill_live_trades_to_state_store.py` 的 SQL 事后订正 hack（:84-99）删净——产品侧已修，订正使命完成。残留：T2 三行级 minors（Low 表留档） | ✅ done（N2） |
| ✅ **【Medium 严重度 · 08-15 登记 · 08-16 清偿】save_plan_legacy JSON 死种子（N3）** | `fc752a75`（2026-08-16）：新建 `tests/_plan_seed.py` **单一归宿**（`seed_plan(date_iso, orders, *, confirmed=True, json_mirror=False, account_id=None)`——DB trade_event(SIGNAL, meta 全量) [+CONFIRMED]，account_id 默认跟生产 `_resolve_account_id()` 可显式覆盖，json_mirror 保 attribution/snapshot 老断言路径）；三处手写同构迁移（`test_probabilistic_broker._seed_plan_truth` / `test_e2e_long_cycle._fake_run_eod_phase` / `test_table_snapshot` 内联）——**meta 逐字节保形实证**（同输入产出与原手写公式一致、事件序不变）、手写散点 grep 清零；DG-5「JSON 对 load_plan 恒不可见」红线写进模块头 docstring。`_legacy_plan_io.py` 保留（纯 JSON 流测试仍用）。Low 清单④「seeding 第三份拷贝」同步销账 | ✅ done（N3） |
| 🆕 **波次遗留清理清单（debt/full-wave-0815 ledger 转录）** | ① **T13-C 未竟维度**：D5/D6/D7/D9 未按原编号整体落地（部分语义散落 T13-A/增量脚本，T13 工单已诚实注记不虚销）——建议与停牌真值工单同批；② `engine._submit` 零内部调用者（W1-B 后遗留）——✅ N5 已删（phases 直取 gateway_service 真身，13 处测试 patch 迁 `phases.<消费方>._submit` / gateway_service 真身）；③ `_ORDER_TIMEOUT` 一全局三拷贝（qmt_connection/io/business from-import 副本，运行期调超时踩三处）——✅ N5 收口（io/business 改 `qmt_connection._ORDER_TIMEOUT` 调用点模块属性访问，patch 统一指契约根，02-module-dependencies 同步订正）；④ seeding 同构第三份拷贝宜抽共享 helper（T15）——✅ N3 处置（`tests/_plan_seed.py` 单源，三处手写归一）；⑤ `_is_trading_day` 与 `trading.calendar.is_trading_day` 一行镜像双份（T9 刻意、注释互引）；⑥ `method_v0.py:328/337` 识别层 rr 门控价位副本（语义不同未收编，T7 记录）；⑦ `strategies/neckline/price_levels.py` 不在 `compute_unit/hashes.py` ENGINE_FILES（指纹未罩住新真身，T18 决策）——✅ stale 勘误（T18 波次已完成收录，本项登记滞后）；⑧ T7/T8 ADR「待用户终签」手续债（决策内容已被两波次实跑约束）；⑨ CR-8 残留：TerminalLogs SSE 接 read-cookie（live 配 token 静默 401）——✅ N5 接线（onMounted 先 POST `/api/v1/auth/read-cookie` 换 HttpOnly cookie 再开 EventSource，吞错直连容错；spec 补 `vi.mock('@/api/client')` + 时序/容错 2 用例）；⑩ 盘中熔断同享 T-1 兜底（T8 follow-up，防盘后启动首轮假阳性 halt）——✅ stale 勘误（CR-3 终审 I-1 已修：stop_loss 读 start None/≤0 先走 T-1 close 兜底，见上方 High 表 CR-3 行）；⑪ `manage_ops_schtasks._schtasks` 在 PYTHONUTF8=1 下 GBK 解码炸 reader 线程（注册成功但 stdout 丢）——✅ N5 修复（subprocess.run 显式 `encoding="utf-8", errors="replace"`，解码永不抛 + 用例锁定 kwarg）；⑫ 仓库孤儿 stash `!!GitHub_Desktop<master>` 择日人工清点（T12 事故记录）；⑬ DC 死代码线索（消息重复/pro 死参）**线索穷尽未复现**（T17 grep：消息重复仅 notifier 幂等守卫与 broadcast 去重设计；「死参」命中全为 min_rr P4 复活史述；vitest.config/.env.example 注释 T1/T5 已修）；⑭ **盘中/盘后熔断判定骨架双份**（`stop_loss._check_portfolio_loss_limit` ⇄ post_close 熔断段共享读基线→query_asset→判定→halt→告警六步骨架，分支语义有意分化）——改判定逻辑需双点同步，漂移风险；后续可抽共同骨架函数参数化 halt 策略；⑮ **QuoteBlackoutThrottle 与 PortfolioBreakerThrottle 同构**（lock+check+mark+reset）——可参数化合一，YAGNI 暂缓 | ②③④⑨⑪ N5/N3 处置 + ⑦⑩ stale 勘误；余项各源工单 follow-up |
| 🆕 **【N5 · D 批】repair/scan 加固 + T13-C 未竟维度 disposition（2026-08-16）** | **D 批四件**：① unfillable sidecar 原子写（tmp+fsync+replace，G5 纪律）+ classify checkpoint 落盘前 re-load 按 (symbol,start,end,reason) 四元组合并（T1b 评审 Important——classify 1h 长跑与 repair --auto 并发写窗口收口，「无并发写场景」旧表述订正）；② `--clear-unfillable --reason` 定向清除（只清 probe_zero_day 推定，不动 symbol_absent/market_empty 铁证）；③ classify 中断路径（限频/超时/守卫）改非零退出码（防 schtasks「半途显示成功」）；④ 现代交易日（≥2005）中点市场级全零守卫——源侧故障判定零标记 + warning + 中断（防单日故障空响应批量误标）。**T13-C disposition（对照 T1/T1b 实际覆盖，不虚销）**：D5 复权一致（scan 维度）**仍未覆盖**——T1/T1b 只做停牌真值/unfillable，前复权一致性仍仅在 sync_daily_incremental 写路径内建；D6 列完整**仍未覆盖**（无列级完整性 scan 维度）；D7 跨湖时区**仍未覆盖**；D9 行数阈值告警**部分语义已存在**（T13-A freshness 行数骤降 + 本批 ④ 市场全零守卫同族）但未作为 scan 独立契约成项——四项转 data 完整性后续工单 | ✅ D 批已处置（本波 N5）；T13-C 余项留档待领 |
| 🆕 **【N 波 · 2026-08-16】遗留 minors + 运行态观察项（如实挂账，不虚销）** | **T5 四项**：① TerminalLogs await 期间组件卸载竞态 ES 泄漏（disposed 标志位）② sidecar 残余毫级并发窗（tmp 加 pid + replace retry）③ isascii 宜函数顶一次性闸（现态①②内联，全角洞已钉死但重复判定）④ `_merge_save` docstring 措辞。**T1 留档三项**：CLI 停牌合法跳空标签含 unfillable 失真（口径混叠）/ 启发式段粒度 len(seg) vs len(unjustified) 口径留档 / `GapRange.__post_init__` 隐式回填地雷。**T2 两项**：8 位形态直接断言 / 形状校验层自测（均三行级）。**T3 两项**：guarded upsert 语义收窄（零实际漂移）/ account_id 空串 or 判空（理论态）。**T4 一项**：专测真函数体泄漏 log 句柄（GC 兜底，既有行为）。**运行态观察**：① 13,535 段 probe_zero_day **推定**不可补（真补需换数据源；`--clear-unfillable --reason` 可重置推定）② 02:00 daemon 首跑（15701 完整性闸）放行与否待实证 ③ classify 推定占比 98.4%——中点有行而段内其余日无行的混段会被低估（方向安全：留 --auto 兜底） | 待领（各源工单 follow-up） |
| ✅ **【CR-4 残留】收盘快照无效值路径静默** | review 后续修复（2026-08-16）：post_close 快照段「query 成功而 total=None/≤0」原无日志无告警静默跳过（与 except 分支同掏空次日 T-1 兜底基线）——补 else 分支 `logger.warning` + live 推 `_alert_critical`（dry_run 不推同口径），+1 测试（query_asset 返 {} 非异常路径，照 snapshot 失败有声先例） | ✅ done |
| ✅ **过时文档 data_pool.md / caisen-methodology-summary.md** | 丙删早已执行（Phase 0 收尾，CR-9 复核确认） | ✅ done |
| ✅ **【CR-11】文档漂移族** | `6f70faa6`+`bc69d546`（T5 · 2026-08-15）：guardrails.md 刷新（测试数 1892 截至标注 + 路径全订正 + §四 E2E 重写 + 新增 §六 fail-closed 语义与**风险取向显式声明**）；data-source-of-truth.md 刷新（7 项巡检实况 + QuanterAudit 调度 + Phase C 语义订正，评审修复清退红线方向写反）；`.env.example` 路径订正。#1/#3/#7/#8 已于 08-14 订正 | ✅ done（T5） |
| ✅ **【测试流程】风控闸变更未同步测试** | 已收口（T1 期清 + 时间依赖测试 mock；T15 存量红真因钉死=save_plan_legacy 死种子而非日期敏感——诊断偏了一层的实证）。**全量红 = 0**（T15 后 1943 passed + 0 failed） | ✅ done（T15） |

## 销账对照速查（full-wave T1-T17 + new-debt N1-N6 → CR/遗留/新债项）

| CR/遗留 | 处置 | 落地 commit |
|---|---|---|
| CR-1 discovery 死页 | ✅ 直返化 + 形状守卫入 gate② | `5d999375`（T1） |
| CR-2 价位双份 | ✅ price_levels.py 单源 + golden | `fbbeb82a`（T7） |
| CR-3 盘后闸 | ✅ 评估点前移 5min 节流 | `0072e1a1`（T8） |
| CR-4 curr_equity fail-open | ✅ live fail-closed + 快照有声 | `7296e3f3`（T2） |
| CR-5 防超卖>防漏挂 | ✅ 反向扫描+口径+CHECK（+T17 集合补全/ro 连接） | `6d434ce4`（T3）+ 本 T17 |
| CR-6 补采回路 | ✅ 部分落盘+原子入列+限频（实弹 350/350） | `4b636756`+`3abea439`（T6） |
| CR-7 巡检无调度/单通道 | ✅ QuanterAudit 16:05 + LocalFileChannel | `42e3bd96`（T4） |
| CR-8 观测面断链 | ✅ sector 删+孤儿路由显式处置（残留 read-cookie 前端接入登记） | `e6100a79`（T16）+ T17 注释订正 |
| CR-9 工单失同步 | ✅ 8 工单回填 + MAP 重写 | `e6100a79`（T16） |
| CR-10 CI 无元守卫 | ✅ ci-heartbeat + 周一 schedule | `6f70faa6`（T5） |
| CR-11 文档漂移 | ✅ guardrails/data-source-of-truth/.env.example | `6f70faa6`+`bc69d546`（T5） |
| TD data→trading | ✅ 边清零（#2 复跑 = 0） | `ddb9c9a9`（T9） |
| W1-B re-export | ✅ 块删除 + gateway 顶部化 | `e2a6153f`（T10） |
| CN T9/T10/T11 | ✅ 探针 + 嵌套去重 + connect 留痕 | `299ab2de` / `710d9c30` / `bf55c6ae` |
| Q/TB（W2-H1/H2） | ✅ broker 四文件 + 回调 Ports | `e3195df0` / `7be0419f`（T13/T12） |
| M2 读写键 | ✅ actual_sid 单源 + is_vetoed + StopLossContext | `ccaad5c9`（T14）/ `710d9c30`（T11） |
| SS（T6 工单） | ✅ 四决策定稿 + 落地 | T16 回填 + T3/T11/T14 |
| 测试存量红 | ✅ 真因钉死（死种子），全量红=0 | `a4e5cc40`（T15） |
| 波次门 L3/L4/perf | ✅ 26 绿 / 双跑零 diff / +1.9%≪10% | T15（无独立 commit，报告实录） |
| N1/N1b 停牌真值【High】 | ✅ 日级判定+共识启发式+sidecar+探针分类（unjustified 16,371→0）+ 根因叙事勘误 | `87e4132d` + `fa4e9c86` |
| N2 entry_date【Medium】 | ✅ traded_time 四态解析锁成交日 + backfill hack 删净 | `076aa44b` |
| N3 死种子【Medium】 | ✅ tests/_plan_seed.py 单源，三处手写归一 | `fc752a75` |
| N4 pytest spawn【测试卫生】 | ✅ _scan_and_spawn_repair 抽锚 + 六用例整锚 mock | `e2e5c957` |
| N5 Low 批 | ✅ ②③⑨⑪+D/E 批 + ⑦⑩ stale 勘误（本表上方 Low 清单行） | `a550c4f2` + `d4c8a754` + `379b90cd` |

## 非痛点（明确不在债内 — MAP Out of scope）

- `broadcast` / `config` / `discovery` / `experiment` / `ops`：非痛点模块，仅当三维扩展（[T3](../../plans/wayfinder/T3.md)）要求时由适配层工单驱动改造。（`compute_unit` 原列于此，2026-08-18 整体退役【ADR-17】，不再在册。）
- 颈线法策略算法本身（缺口在 [neckline-algorithm-gaps] memory 独立跟踪，非架构债）。**边界澄清**：CR-2 已收口「同一算法两份实现」的工程 SSoT 债；算法有效性问题（regime/期望/Kelly）属 A 波——**仍是当前最高优先空洞**（[roadmap 里程碑](roadmap.md)）。
