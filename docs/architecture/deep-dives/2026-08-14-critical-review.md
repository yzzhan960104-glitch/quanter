> 最近复核：2026-08-16（新债清偿补记）· 维护者：new-debt-wave session ·
> 证据基线：HEAD `2e74cb9d`（master，领先 origin 1 commit）。三路并行审计（前端契约 / 风控链路 / 治理波次）+ 复跑 [#2](../02-module-dependencies.md) 依赖扫描 + 逐项 git 考古。
> 性质：**带日期的快照评估**（非活文档）——批判结论有时效，下次大波次合并后应重估或在文头标注过期。
> 权威归宿：本文是「架构健康度批判评估」叙事的单一归宿；债务条目登记与严重度判定归 [#6](../06-tech-debt.md)（本文 CR-\* 与 #6 新增条目一一映射）；波次工单全景与状态归 [roadmap](../roadmap.md)。本文不重抄规则，只链。

# 2026-08-15 清偿后记（debt/full-wave-0815 波次）

**本复核登记的 CR-1..11 已由技术债全量清偿波次（分支 `debt/full-wave-0815`，T1-T18，2026-08-15）逐条处置。** 终态对账单（含新债登记）见 [#6](../06-tech-debt.md) 销账对照速查；波次逐 task 证据在 `.superpowers/sdd/2026-08-15-tech-debt-full-wave/`。一行处置表：

| CR | 处置 | 残留 |
|---|---|---|
| CR-1 | ✅ T1（`5d999375`）discovery.ts 直返化 + 形状契约守卫入 gate② | 守卫单行正则局限（成文） |
| CR-2 | ✅ T7（`fbbeb82a`）`strategies/neckline/price_levels.py` 单源 + golden 钉死 | method_v0 rr 门控副本（语义不同，Low 清单） |
| CR-3 | ✅ T8（`0072e1a1`）盘中 5min 节流评估点前移，emergency_halt 不停调度 | 盘中无 T-1 兜底（follow-up）；lock_down 后 monitor skip 语义已入 guardrails |
| CR-4 | ✅ T2（`7296e3f3`）curr_equity 缺失 live fail-closed + 收盘快照失败有声 | live 停调度=行为变更（运维知悉，guardrails §六） |
| CR-5 | ✅ T3（`6d434ce4`）反向扫描 + 孤儿口径 + fill.direction CHECK；T17 补集合四 action + mode=ro | 取向本身显式入 guardrails（选择保留） |
| CR-6 | ✅ T6（`4b636756`+`3abea439`）部分落盘 + 原子入列 + 限频，实弹 350/350 零中断 | **→ 新债【High】停牌真值缺口**（16371 段含真缺永占配额） |
| CR-7 | ✅ T4（`42e3bd96`）QuanterAudit DAILY 16:05 + LocalFileChannel 双通道 | 心跳/调度依赖运行态持续观测 |
| CR-8 | ✅ T16（`e6100a79`）sector 删 + 孤儿路由显式处置（training/research=桥消费保留，ops/processes=内部观测，review/diagnose=CLI 写面） | TerminalLogs SSE 接 read-cookie 未做（Low 清单） |
| CR-9 | ✅ T16（`e6100a79`）8 工单回填 + MAP frontier 重写（余 T3）+ sdd G7/G8 补账 | 状态同步机制化靠波次收尾 checklist（本 T17 即三件套） |
| CR-10 | ✅ T5（`6f70faa6`）ci-heartbeat 元守卫 + 周一 schedule 心跳 | 基线依赖 master 首次成功 run（T18 push 后建立） |
| CR-11 | ✅ T5（`6f70faa6`+`bc69d546`）guardrails / data-source-of-truth / .env.example 刷新 | 测试数漂移约定「改体量时顺手更新截至标注」 |
| 新债（08-15/16 登记，#6 NEW 组） | ✅ **debt/new-debt-0816**（N1-N6 · 2026-08-16）：High 停牌真值 N1 `87e4132d` + N1b `fa4e9c86`（日级判定+共识启发式+unfillable sidecar+探针分类，unjustified 16,371→0）；Medium entry_date N2 `076aa44b` / 死种子 N3 `fc752a75`；测试卫生 pytest spawn N4 `e2e5c957`；Low 批 N5 三笔（`a550c4f2`/`d4c8a754`/`379b90cd`）。**根因叙事勘误：CR-6 行登记的 000029.SZ 铁证不成立**（scan_pre_t1 实证 `suspend_justified=True`，从未进 unjustified 集；真根因=2019-2022 suspend_d 稀疏 + 段级 all() 放大 + 920xxx.BJ daily 不载） | 13,535 段推定不可补（真补需换数据源）；02:00 daemon 首跑待观察；N 波 minors 留档 #6 Low 表 |

**对总评三错位的清偿面**：错位 2（策略数学零单源）——CR-2/T7 已收口；错位 3（风控命名语义）——CR-3/T8 + CR-4/T2 已收口；错位 1（基础设施与策略证据倒挂）**未变**——A 波仍未动工，仍是最高优先空洞（[roadmap](../roadmap.md) 里程碑重申）。**本文批判性结论的有效性**：§3 各 CR 章节保留为历史快照（根因/证据链仍有档案价值），但处置状态以本后记与 #6 终态为准。

# 2026-08-14 架构批判性复核

## 0. 结论摘要

**总评（诚实口径）**：这套系统的**结构工程与治理纪律**在同类个人量化项目里处于显著高位——13 包分层清晰、数据 9 域 SSoT、1821 个测试函数、L1/L3/L4 三层验证纵深、G 波刚完成一轮高质量的 fail-closed 安全迁移。但把它放回「它为什么存在」的语境里审视，**最尖锐的问题不在任何单个模块，而在三个系统性错位**：

1. **基础设施成熟度与策略证据成熟度倒挂**（见 §4.1 张力一）：42.1k 行代码、240 个文件、三套工单体系，服务的是一个**可信度尚未立住的单一策略**——A 波（策略可信：regime 闸 / walk-forward 判定 / 成交真实性 / Kelly 收敛）在审计 spec 里被显式跳过、至今未动工，roadmap 的「可上 live 判据」仍是「待量化」四个字。**演进没有停损点**。
2. **回测-实盘等价性这一自称的头号资产，其数学基础没有单源保护**（见 CR-2）：入场价位 `stop/tp1/tp2` 在 `strategies/neckline/backtest.py:149-151` 与 `trading/compute/plan.py:141-146` 各算一遍、参数取自不同配置口。数据有 9 域 SSoT + BANNED 静态守卫，**策略数学零守卫**——改公式漏改一处，回测结论静默失效。
3. **风控的命名与语义不符**（见 CR-3）：`check_daily_loss_limit` 全库唯一调用点在 **15:30 的 post_close**。「日内 -3% 熔断」的实际语义是**盘后确认、次日停手**——盘中组合级回撤在任何时刻都不会触发任何动作。对 A 股 T+1 单日可击穿 3% 的品种，这条链的盘中时效性保护为零。

**本次复核的方法论立场**：批判的价值取决于事实精度。本文每条发现均附 file:line 证据；对「已修复」的旧指控（熔断基线 fail-open、caisen 死视图、CI 断链）**如实销账**，不为了批判的爽感而复述过期问题。

### 六维健康度

| 维度 | 评级 | 一句话判定 |
|---|---|---|
| 包结构 / 依赖方向 | **优** | L0-L5 分层稳定，W1-A 后 trading 零反查，#2 复跑仅 2 条新次要边 |
| 数据纪律 | **优−** | 9 域 SSoT + 写入守卫 + scan gate 体系完整；但 15701 段漏采的补采回路熔断停摆、巡检脚本无人调度 |
| 验证纵深 | **中+** | L1/L3/L4 + golden + engine_hash 实打实；但 CI 曾静默死亡 19 天且无元守卫，契约 gate 只比 URL 不比形状 |
| 安全姿态 | **优−** | G 波完成 fail-closed 迁移（鉴权/熔断/原子写/WAL/超时/幽灵单），带测试钉死；但观测单通道押在 fire-and-forget 钉钉上 |
| 风控语义 | **中−** | 判定层 fail-closed 已收口；但「盘后闸」语义、curr_equity 方向 fail-open、「防超卖>防漏挂」全链取向三题并存 |
| 策略证据 | **弱** | A 波未动工，min_rr 复活后 trial 池仍薄（88→146），颈线法可上实盘的前提条件一个未验证 |

---

## 1. 事实基线：架构文档未曾入账的两波演进

8 视图集最后复核 2026-08-08~12。此后 master 上落地了**两波大演进**，架构视图集零更新——本节先把代码实况补进架构叙事，这是后续一切批判的地基。

### 1.1 G 波：保护链 fail-closed 迁移（08-13~14，12 commit）

源自《全项目审计治理 spec》（`docs/superpowers/specs/2026-08-13-audit-remediation-design.md`）。主题：把七类「静默失效时继续放行」的保护链全部翻成 fail-closed。

| commit | 主题 | 架构含义 |
|---|---|---|
| `f445fe71` G1 | CI 复活（ci.yml 三处路径 + 防漂移自检 step） | 验证门从「叙事」回到「实况」（见 [CR-10](#cr-10medium-验证效力-ci-曾静默死亡-19-天且无元守卫)） |
| `cf41d973` | 删 caisen 死视图（14 文件，−1943 行） | #6 的 Low 债销账；但接棒首页当天患新病（[CR-1](#cr-1critical-用户面研究动线第一入口是-http-200-的死页面)） |
| `5dab465d`+`e7cf2c8c` G2 | 鉴权 fail-closed + SSE cookie + host 127.0.0.1 + /health 去指纹 | 安全面从「默认开放」翻成「默认拒绝」；live 无 token 拒起（`trading/__main__.py:371-375`） |
| `8d4ef714` G3 | 熔断基线缺失 fail-closed + account_daily T-1 兜底 | **#3/#6/#8 挂着的 Critical「start 漏采→裸奔」就此销账**（详见 §5.1） |
| `542293fc` G4 | 外部 SDK 超时注入（tushare/FRED/calendar/xtdata） | TCP 挂起从「无限等」变「可观测超时」 |
| `07932b40`+`5df74c03` G5 | 数据原子写（tmp+fsync+replace）+ schema 迁移改备份回灌 | 湖文件写入获得崩溃安全 |
| `d92d74d8` G5 | Windows fsync 只读句柄修复（"rb"→"r+b"） | **G5 首版原子写在 Windows 上根本没生效**——同波自纠 |
| `4735681b`+`5f59a504`+`b3dec79b` G6 | SQLite WAL+timeout 基线 + `insert_order` 返 False 中止 `_submit` | 修「DB/柜台脱节幽灵单」；并发写基线对齐 |
| `4a29362f` G7 | 告警降级可观测 + 订单终态集单源 + FSM 写校验 | 观测层补洞（但 task 报告缺失，见 CR-9） |
| `2e74cb9d` G8 | L2 sid 轮换前置「客户端可服务」闸 | 三件套探测避免 2 分钟白耗 2.7GB down_queue；**此 commit 尚未 push** |

### 1.2 P0-P6 策略优化波（08-12~13 合入 `cc4629b7`）

回测性能与方法论大版本：P1 识别热路径向量化（720s→35.5s）、P2 TPE batch、P3 敏感性分析后台化（新增 `/research/discovery/*` 4 端点 + DiscoveryLabView——[CR-1](#cr-1critical-用户面研究动线第一入口是-http-200-的死页面) 的载体）、P4 min_rr 死参复活（ADR：`docs/2026-08-13-p4-min-rr-adr.md`）、P5 walk-forward 四折锚定交叉验证、P6 数据可信度指纹。discovery 包 2942→3645 行。**这是「策略验证方法论」的实质投资**——但它恰恰放大了 T1 张力：验证工具越精良，越没有理由回避「把策略本身验掉」。

### 1.3 时间差诊断

两波演进各有 spec/plan，但全部落在 `docs/superpowers/` 体系；`docs/architecture/` 的维护协议（「改代码影响某视图时同一改动内更新该视图」）**在快速波次中失效**——没有哪个工单的验收门里有「刷架构视图」这一项。结论：**两套文档体系出现双真相源裂缝**，架构视图集的「最近复核」日期给人保鲜感，实际滞后两波。治则：把「波次收尾刷 #2/#6/roadmap」写进波次 plan 的收尾 checklist（见 §6 建议 5）。

---

## 2. 架构资产盘点（批判的前提是先承认做对了什么）

| # | 资产 | 证据 |
|---|---|---|
| 1 | **分层稳定、方向干净** | 13 包 L0-L5；W1-A 后 `trading→presentation` 边权 0；本次复跑全量 import 扫描仅新增 2 条次要边（`broker→ops:1`、`discovery→data:1`），`data→trading` 2→1（M1 日历下沉生效） |
| 2 | **数据 SSoT 纪律** | 9 域真相源 + BANNED 静态守卫（CI 时炸）+ 写入守卫 safe_overwrite + scan gate + P6 数据指纹；同类项目罕见 |
| 3 | **事故驱动修复的沉淀率** | fill `UNIQUE(order_id,traded_time)` 幂等（08-04 事故）、session lock（connect -1 双进程教训）、事件驱动 `pipeline_then_eod`（时钟赌博教训）、clock.py 单一时间源（T+1 错位教训）——每个事故都变成了结构性修复而非补丁 |
| 4 | **验证纵深** | ~1821 测试函数（08-12 全量绿口径 1687）+ e2e_long_cycle 26 测 + L4 双跑方法论 + golden 等价守护 + engine_hash |
| 5 | **G 波安全迁移质量** | 每项整改带专门测试钉死（如 `tests/trading/test_breaker_fail_closed.py`：live halt ×3 / dry 停手 ×2）；DG 决策门留痕（spec §DG-G2/G3 裁决原文可查） |
| 6 | **依赖纪律** | ADR-10 拒引入清单 + 2026-08-11 六大框架评估（`docs/2026-08-11-framework-evaluation-decision.md`）逐个给出不可迁移资产论证，关闭重复调研 |
| 7 | **诚实文化** | P4 ADR「未完成（诚实标注）」小节；audit spec 对旧结论的考古订正（min_rr「死参数」结论的失效分析）；本复核亦承此风 |
| 8 | **回测-实盘同构** | `decide_exit` 离场决策单源 + 无前视纪律 `df.loc[:T]`——这是框架评估中自认的四类不可迁移资产之首（但见 [CR-2](#cr-2critical-策略数学-入场价位三件套双份实现参数通道异构)：入场价位不在保护内） |

---

## 3. 批判性发现（CR-1 ~ CR-11）

排序按严重度。每条含：现象 → 证据 → 根因 → 影响 → 治理归宿（已识别未动工 vs **本次新发现**）。

### CR-1（Critical·用户面）：研究动线第一入口是 HTTP 200 的死页面

- **现象**：caisen 退役后，`/discovery`（DiscoveryLabView）是策略研究的首页与 P0-P6 全部投资的用户面出口。它**永远空态**：HTTP 200、无报错 Toast、骨架屏正常渲染。
- **证据链（三环相扣，已逐一亲验）**：
  1. `presentation/web/src/api/client.ts:62` 响应拦截器 `(response) => response.data`——facade 拿到的已是业务 payload；
  2. `presentation/web/src/api/discovery.ts:65,72,79,84` 四个函数却写 `const { data } = await apiClient.get(...)`——对 payload 再取 `.data` 得 `undefined`（对照 trading.ts/macro.ts/data.ts 均直接 return，唯独 discovery.ts 多剥一层）；
  3. `DiscoveryLabView.vue` `loadAll()` 中 `p.param_space` 对 undefined 取属性抛 TypeError → 被空 `catch {}` 静默吞掉 → 空态渲染。
- **为什么所有护栏都没拦住**：契约 gate（`ops/check_contracts.py`）**只比 URL + method，不比响应形状**；`DiscoveryLabView.spec.ts:11` 用 `vi.mock('@/api/discovery')` 把被测集成点 mock 掉；HTTP 200 使 axios 错误拦截器（Toast）不触发。**三重盲区**。
- **根因**：P3 波新增 facade 时复制了「带壳」时代的旧习惯；更深一层——**技术债账本以「代码是否存在」记账，用户可用性以「数据是否到达屏幕」结算**，两套账没有对账机制。
- **影响**：策略验证成果（敏感性/热力图/参数/搜索进展）对用户不可见；且这是「沉默死视图」的**复发**——caisen 死了约一个月才删（404 形态，响亮），接棒页面当天就病（200+undefined 形态，**更隐蔽**）。
- **归宿**：**本次新发现**，任何工单未识别。一行修复（去掉二次解构），但应连带把「响应形状契约」纳入 gate（见 §6 建议 2）。

### CR-2（Critical·策略数学）：入场价位三件套双份实现，参数通道异构

- **现象**：数据有 9 域 SSoT；策略数学没有。入场价位 `stop / tp1 / tp2` 两处独立计算：
  - `strategies/neckline/backtest.py:149-151`：`base_stop = c_star − id_cfg["stop_atr_mult"]·ATR`、`tp1 = c_star + exec["tp1_h_mult"]·H`（回测侧，参数走 `id_cfg`/`exec`）；
  - `trading/compute/plan.py:141-146`：`take_profit = neckline + tp_mult·h`、`tp1_price = neckline + tp1_mult·h`（实盘计划侧，参数走 `stop_cfg`）。
- **根因**：历史演进程中两侧各自生长；`decide_exit`（离场决策）做过单源化，但**入场价位从未收口**。参数通道也不同源（`EXEC_DEFAULTS` vs `stop_cfg` 六层默认值），这正是审计 spec C1「配置六层默认值 SSoT」与 A5「价位单源」要治的——**两项均未动工**（`grep compute_price_levels` 零命中）。
- **影响**：回测-实盘等价性是本项目自称的四类不可迁移资产之首（框架评估决策 §统一依据 1），但它的**数学地基靠人肉同步**。改公式漏改一处 = 回测结论静默失效而实盘继续执行——这是比任何单点 bug 更高阶的风险形态：**它不报错，它只是让「你以为的验证」作废**。
- **归宿**：审计 spec 已识别（A5/C1），未动工。本复核将其从「待办」升格为 **Critical**：等价性资产上的缺口，严重度应与资产价值成正比。

### CR-3（High·风控语义）：「日内熔断」实为盘后闸

- **现象**：`check_daily_loss_limit`（`trading/compute/breaker.py:40`）全库**唯一**生产调用点是 `post_close.py:319`，而 post_close 的 cron 是 `30 15 * * mon-fri`（`engine.py:300`）——**收盘之后**。盘中 30s 一档的 stop_loss 只做 per-position 止损/超期，不做组合级权益检查。
- **实际语义**：「日内 -3% 熔断」= 盘后确认当日亏损 → `emergency_halt` 粘滞锁 → **次日**拒新单。盘中组合级回撤不触发任何动作。
- **影响**：A 股 T+1 下单日 10%/20% 振幅完全可以击穿 3%；闪崩场景这条链的时效性保护为零。audit spec A4 自己也承认「组合级回撤保护仅 -3% 单日一闸」——**已知但未被架构文档表述**：#4/#8 把它记成「post_close 盘后对账」的一项，没有任何视图指出「盘中无组合级保护」这个语义空洞。
- **归宿**：半新发现（spec 承认、视图未表述）。建议要么改名（`daily_loss_review`）让名字诚实，要么把评估点挂进 stop_loss 巡检（30s 一档，读 `gw.query_asset` 有限频代价——需权衡），至少 #4/#8 要把语义写穿。

### CR-4（High·风控对称性）：curr_equity 缺失方向仍 fail-open 且静默

- **现象**：G3 把**基线缺失**（start_equity）收成 fail-closed 了，但**当前权益缺失**（curr_equity）仍是 fail-open：`post_close.py:308-314`，`query_asset` 返 None/≤0 或抛异常 → `breaker_skipped=True` + 仅 `logger.warning`——**live 也不推 CRITICAL、不停调度**。
- **讽刺点**：query_asset 失败最可能的原因恰是断线/网关锁死——**正是熔断最该在岗的异常环境**。这与 DG-G3 裁决「不选仅告警不动作」的精神直接相悖，是 G3 留下的对称性缺口。同函数 reconcile 段 broker 失败在 live 是推 CRITICAL 的（`post_close.py:218-222`），唯独熔断段不是——函数内部双标。
- **关联**：post_close 收盘快照失败（六段软降级之一，`:431-445`）会静默掏空**次日**的 T-1 兜底基线——单日故障被软降级吸收，连续两日才 fail-closed，中间这一天熔断基线用的是隔夜近似值。
- **归宿**：**本次新发现**（G3 验收测试只钉了基线方向）。修复便宜：curr_equity 缺失在 live 对齐基线方向的 CRITICAL + `_CriticalHalt`。

### CR-5（High·系统性偏差）：「防超卖 > 防漏挂」的全链一致取向

- **现象**：这不是单个分支，而是**三个层次同向**的风险哲学：
  1. **交易层**：DB 查失败视为「TP 已挂」——`stop_loss.py:388-389` `_tp_ok = True`、`order_state.py:521-523` `_tp_already = True`（注释自认「宁可漏挂人工补，不超卖」）；止盈/止损卖单被拒只计数 L2 聚合，不重试不升级（`order_state.py:530-532`、`stop_loss.py:411-416`）。
  2. **巡检层**：`audit_ssot.py:109` fill↔position 对账的扫描集是 `SELECT ... FROM position WHERE qty != 0`——幻影持仓方向（超卖风险）会告警；**真实持仓漏记方向（fill 净额≠0 但 position 行缺失/为 0）的符号根本不进循环，静默 PASS**。
  3. **schema 层**：`fill.direction` 无 CHECK 约束（`state_store.py:364`），任何非 `'BUY'` 值（拼错/小写）一律按 −qty 净额参与对账（`audit_ssot.py:106`）。
- **批判**：08-04「1 笔成交记 24 次」事故后，系统性地加固了超卖方向——合理。但**漏挂方向的敞口同样真实**（持仓漏记 → 止损/止盈漏挂 → 裸奔），且它在交易层和巡检层**同时**沉默。风险取向可以是选择，但前提是**被表述、被观测**——现在它只是三处代码注释里的一致巧合。另外巡检孤儿 SIGNAL 检查的后续 action 集合有口径漂移（含从未写过的 `OPEN`，漏 `ORDERED/TP1_FILLED/TP2_FILLED/STOP_TRIGGERED`，`audit_ssot.py:141-166` vs `order_state.py:439,475`）。
- **归宿**：**本次新发现**（「方向矛盾」旧说法不成立，实为单向盲区）。建议：巡检补一个反方向扫描（fill 净额≠0 且 position 缺行）；`fill.direction` 加 CHECK；取向本身写进 `docs/guardrails.md` 显式声明。

### CR-6（High·数据债）：15701 段漏采，补采回路熔断停摆

- **现象**：`logs/repair_auto.log`（GBK）记录：完整性 scan 发现 **15701 段漏采（390 标的）**，自动补采受配额 `MAX_REPAIR_SEGMENTS=50`（`data/tools/repair_gaps.py:50`）截断为 50 段；log 尾部显示 **repair 熔断开启（连续 7 次失败，6h 恢复）**——闭环卡死，15701 段未消化。
- **批判**：T13-B 建成的「scan→repair 异步闭环」在架构上是资产，在运行态上是**停摆的资产**。#6 把 data 完整性标成「✅ 全治理」，治理的是**代码路径**，不是**数据本身**。另：工作区 `data_lake/a_shares_daily.parquet` 长期 dirty（湖数据未固化）。「daemon 需按 E 盘重估」即指此回路在 E 盘新环境需重验。
- **归宿**：已知尾巴（记忆线索），但**停摆事实与严重度未入档**。应作为 #6 Critical 项登记：漏采段不补，回测语料的可信度打折扣，P6 指纹会把问题「指纹化」却修不了它。

### CR-7（Medium·观测）：audit_ssot 无人调度 + 观测单通道

- `scripts/audit_ssot.py` 7 项检查（文档宣称 5 项，漂移）：全库无 schtasks / CI / run_checks 挂载——`docs/data-source-of-truth.md:20` 称「调度/手动跑时炸」，但**调度并不存在**。巡检层 = fail-open。
- 告警通道：`_alert_critical` 全吞（`critical.py:58-65`）、钉钉 fire_and_forget daemon 线程仅记日志（`infra/notifier.py:245-250`）——fail-closed **动作**靠进程内 `_halt` flag 可靠，但**人工知情**完全押在会失败的单一通道上。
- 对照组：数据域 discovery daemon 漏采段>0 **拒跑**（`discovery/daemon.py:44-55`，P5-I2 fail-closed）——数据域已收口，交易域巡检未收口，**标准不一**。

### CR-8（Medium·前端）：SSE 鉴权死端 + 结构性恒空 + 孤儿路由

- `POST /api/v1/auth/read-cookie`（G2 补的「设置侧」死端，`main.py:684`）**全前端无人调用**——`TerminalLogs.vue:60` 直接 `new EventSource`。dry_run 无碍；**live 配 token 那天，综合看板日志面板静默 401**。G2 修了后端半截，前端半截悬空。
- `/macro/sector/flow` 后端自述「sectors 恒空（数据源已退役）…彻底下线待前端确认后移除」（`macro.py:46-48`）——宏观驾驶舱板块图**结构性永远空态**，双向搁置。
- 孤儿路由：training 5 端点 + research proposals 5 端点 + `/ops/processes` + `/review/diagnose` 后端健在、前端 facade 缺失（router 自述「重建需配套 training.ts facade」）。

### CR-9（Medium·元治理）：三套工单体系状态失同步

- wayfinder 工单 **T2 仍标 open 但 W1-A 已合并**（reflog 实锤 fast-forward，origin 已含）；**T13 仍标 open 但 T13-A/B 已合并**；`MAP.md` frontier 段仍把 T0.1 列为阻塞 T1 的前沿（T1 done 已两周）。工单账本与现实脱节，后来者按 MAP 领工会领到幽灵任务。
- **G8 编号撞车**：sdd 目录的 Task G8（删 caisen）与 master 的 G8（sid 轮换闸）同号不同物；G7 task 报告缺失；progress.md 止于 G6。SDD ledger 自身欠账。
- **plan-as-written ≠ plan-as-executed**：M4/W0-tail 两份 plan 是 08-14 才补落盘的（`81d6d49c`），实现 08-12 已进 master，41/36 个 checkbox 全空。
- 批判：单人 + AI 协作项目，流程开销已在增长（spec→plan→task→验收→收尾→W0 收尾→补落盘），而**状态回填是这个流程里最常被牺牲的环节**——它不阻塞任何合并，却决定下一轮决策的质量。

### CR-10（Medium·验证效力）：CI 曾静默死亡 19 天，且无元守卫

- 时间线：契约 gate 体系 07-12 诞生（`d4870d07`）→ 07-25 前端目录 `web/`→`presentation/web/` 迁移（`2c49ee57`）打断 CI 两处路径 → **CI 静默死亡** → 08-13 G1 复活（`f445fe71`，带防漂移自检）。期间 T13-A/B、T1 终验、W1-A、M4 的「全量绿」全部是本地口径；caisen 404 死视图存活整月——**设计来抓这类断链的 gate 从未在 CI 里开火**。
- 深层问题：CI 死亡是静默失败（workflow 在、步骤跑不起来），**没有任何机制报告「CI 已 N 天未跑」**。G1 修了路径并加了路径自检（好），但「CI 必须最近跑过」这个不变量本身无守卫。
- 现状注记：master 领先 origin 1 commit（恰是 G8 保护闸）；CI 实际 run 记录本机无 `gh` 未验证——**「复活」是代码事实，「在跑」是待验证**。
- 关联漂移：`docs/guardrails.md` 仍写「533 项后端测试」（现 ~1821 测试函数）、无 DG-G3 任何痕迹——它被 #4/#5/#6 引用，**引用链本身是断的**。

### CR-11（Low·文档漂移族）

- #1「Tushare 唯一数据源（AKShare 已退役）」不准确：宏观社融/DR007 以 akshare 为 fallback（`config/registry.py:39-46`），`data/clients/` 下 akshare / alpha_vantage / yfinance 客户端仍存活，G4 超时注入覆盖 tushare/**FRED**/xtdata。
- #7 与 `data-source-of-truth.md` 均称 audit「5 项检查」，实际 7 项。
- #2 行数/边权漂移（本次已刷，见该文件）；#3 时序图与 #8 时间语义表仍写「eod 写 account_daily.start」——**现行代码中 eod/pipeline 完全不写 account_daily**，start 唯一写入方在 pre_open（`pre_open.py:406-418` 精确抓取 + `:363-404` T-1 兜底）。
- `docs/guardrails.md`：533 测试数 + 无 DG-G3/G 波任何同步。

---

## 4. 结构性张力（比单条发现更根本）

### 4.1 张力一：基础设施复利增长 vs 策略证据缓慢积累

这是最重要的批判。系统的演进速度（T1 拆分 → W0/W1-A → P0-P6 → G 波，8 天四波）远高于策略证据积累速度（min_rr 复活后 trial 池 88→146 仍薄；A 波 regime 闸 / walk-forward 判定 / 成交真实性 / Kelly 收敛**零动工**；颈线法的 regime 过拟合与熊市负期望疑虑未证伪）。roadmap 自己写着「里程碑：可上 live 判据（待量化）」——**演进没有停损点**，而「演进优先于 live」的口号在实践中滑向「基础设施建设成为拖延策略验证的心理出口」。诚实的表述是：这套架构当下服务的，是一个**尚未证明正期望的策略的模拟盘**；它最大的真实风险不是任何技术债，而是**投入与验证结论不匹配**。反向也成立：P5 walk-forward + DSR + P6 指纹是验证策略的正确工具，工具已就位——**该用它了**（见 §6 建议 1）。

### 4.2 张力二：治理速度 vs 文档同步速度

G 波一天 12 commit，架构视图集零更新；两套 spec 体系（superpowers/ 与 architecture/）出现双真相源裂缝；wayfinder 工单状态与现实脱节（CR-9）。治理机制对「写代码」有验收门，对「回填状态/刷视图」没有——于是后者系统性欠账。**文档不是治理的装饰品，是下一轮决策的输入**；输入过期，决策质量衰减（本次复核若直接采信 #6，会把已治项当活债、把活债当不存在）。

### 4.3 张力三：fail-closed 迁移的未完成对称

G 波把「已知清单」翻成了 fail-closed，质量很高；但对称性未闭环：curr_equity 方向（CR-4）、漏挂方向（CR-5）、巡检调度（CR-7）、告警通道（CR-7）仍 fail-open；数据域 daemon 已 fail-closed 而交易域巡检没有（标准不一）。**fail-closed 不是一份清单，是一种默认姿态**——清单式治理天然留下「没列到的方向」。

### 4.4 张力四：单机简洁性 vs 运行时韧性

全部韧性假设押在「一台 Windows 机器 + 单 uvicorn 进程 + QMT 同 sid 独占」上。D/E 盘双安装错位导致 connect -1 的排障史证明：**部署态（.env 指向、安装位置、注册表 env）没有 SSoT，也不在任何巡检范围内**。sid 轮换场景直到 G8 才被认真对待（三件套探测）。T9 探针已落但 T10/T11 未完。这不是要求分布式——是要求**把「这台机器」本身当作一个被观测的系统**。

---

## 5. 与 [#6](../06-tech-debt.md) 债务账本的对照

### 5.1 销账清单（文档挂账、代码已治——本次核销）

| #6 原条目 | 核销证据 |
|---|---|
| Critical「account_daily.start 漏采 → 熔断基线裸奔」 | `8d4ef714`（DG-G3）：判定层 fail-closed（live `_CriticalHalt` / dry 停手 + CRITICAL）+ T-1 兜底回填 + `tests/trading/test_breaker_fail_closed.py` 钉死。**残留**：curr_equity 方向未收口（→ CR-4 新立） |
| Low「前端 caisen 死视图」 | `cf41d973` 整删 14 文件 −1943 行，gate② 绿。**接任者**：discovery 解包 bug（→ CR-1 新立，且升 Critical） |
| （未挂账但记忆在案）CI 路径双错 | `f445fe71`（G1）复活 + 防漂移自检。**新立**：CI 运行态元守卫（→ CR-10） |

### 5.2 入账清单（本次新增，已同步登记进 #6）

| CR | 严重度 | 一句话 | 状态 |
|---|---|---|---|
| CR-1 | Critical | /discovery 死页面（200+undefined+三重盲区） | 新发现 |
| CR-2 | Critical | 入场价位双份实现，策略数学无 SSoT | spec A5/C1 已识别未动工 |
| CR-3 | High | 日内熔断实为盘后闸，盘中组合级零保护 | spec A4 半承认 |
| CR-4 | High | curr_equity 缺失静默跳过熔断（G3 对称缺口） | 新发现 |
| CR-5 | High | 防超卖>防漏挂三层同向盲区 | 新发现 |
| CR-6 | High | 15701 段漏采 + 补采回路熔断停摆 | 尾巴已知、停摆未入档 |
| CR-7 | Medium | audit_ssot 无调度 + 告警单通道 | 部分新发现 |
| CR-8 | Medium | SSE cookie 前端死端 + 恒空端点 + 孤儿路由 | 新发现 |
| CR-9 | Medium | 三套工单体系状态失同步 | 新发现（元治理） |
| CR-10 | Medium | CI 静默死亡 19 天教训 + 无元守卫 | 新发现（元验证） |
| CR-11 | Low | 文档漂移族（数据源宣称/巡检项数/写入方漂移） | 新发现 |

---

## 6. 优先级建议（供 roadmap 编排，不在本文展开方案）

1. **启动 A 波（最高优先）**——不是因为它最容易，而是因为它是唯一决定「这一切值不值」的波。先用 P5 walk-forward + 分年回测回答颈线法是否站得住；若不成立，T2/W2 的优先级应让位于策略迭代。**给「演进优先于 live」装上停损点：定义量化判据（如「分年 Calmar 全正 + walk-forward 折外衰减 <50% + 成交滑点敏感性存活」），判据不过则冻结非安全类演进**。
2. **CR-1 一行修复 + 形状契约**：删 discovery.ts 四处二次解构；把「响应形状」纳入契约 gate（openapi schema ↔ TS 类型的运行时校验，或最小 e2e 冒烟断言「页面拿到非空 payload」），否则沉默死视图必复发。
3. **CR-2 价位单源**（并入 A5/C1）：抽 `compute_price_levels(c_star, H, atr, cfg)` 单函数双侧共用 + 参数单源 + 等价性 golden 测试（同输入两侧同价位）。
4. **CR-4/CR-5 廉价收口**：curr_equity 缺失对齐 fail-closed；巡检补反方向扫描 + `fill.direction` CHECK——两处都是小改，补齐 G3 的对称性。
5. **流程一条**：波次收尾 checklist 增加固定三项——刷 #2（重跑扫描）/刷 #6（销账+入账）/回填 wayfinder 状态。文档同步不是装饰，是下一轮决策的输入。
6. **CR-6 补采回路复活**：查连续 7 次失败的根因（大概率 E 盘迁移后路径/权限/配额），按 50 段/夜的节奏消化 15701 段并固化为 schtasks；期间回测语料标注「含已知缺口」。

---

**相关**：[#6](../06-tech-debt.md)（债务账本·本次同步更新）· [roadmap](../roadmap.md)（波次全景·本次同步更新） · [#2](../02-module-dependencies.md)/[#3](../03-data-flow.md)/[#8](../08-control-time-flow.md)（事实性订正） · `docs/superpowers/specs/2026-08-13-audit-remediation-design.md`（G/A/C 波权威） · `docs/superpowers/specs/2026-08-11-tech-debt-governance-master-design.md`（W 波权威）
