# QMT/东北证券全面退役方案——东财掘金升格唯一实盘平台与数据源

> 日期：2026-08-27 ｜ 状态：**P0-P3 全部执行完毕**（同日；P2 冷待机期由用户指令豁免）
> 执行轨迹：P0=de19b6dd（看护/采集/晨检）→ P1=46c3895d（研究面-only 闸+/live 退役）→ P3=8ff0b839（QMT 执行层删除+ops_sched 研究面接管）
> 锚点分支：archive/qmt-stack-final（删除前完整状态）
> 决策背景：掘金腿 R6-13 系列收官（端到端成交验证 + 五项自愈机制），用户裁决掘金升格为主交易平台，全面废弃东北证券 QMT 栈，掘金为唯一【实盘】数据源。
> **边界假设（默认口径，见§6 裁决点）**：tushare 研究主湖（data_lake，回测/发现/信号质量研究）**保留不动**——它与 QMT 栈无关；"唯一数据源"指实盘链路（行情/日历/tick/交易）不再有 QMT 通道。

---

## 一、目标终态

```
┌─ 研究/回测/发现（不变）────────────────┐   ┌─ 实盘（全新单栈）──────────────────┐
│ tushare 主湖 + 本地回测内核 + diag 循环 │   │ 掘金终端(emgm3) = 唯一平台          │
│ （engine_hash 指纹体系照旧）            │   │  ├ 数据：gm SDK（parity 已验证）    │
│                                        │   │  ├ 交易：gm SDK → gmterm-serv:7001 │
│                                        │   │  ├ 运维：7002 API + relaunch ps1   │
│                                        │   │  │       + goldminer-terminal skill │
│                                        │   │  └ 观测：audit CSV→DB + 截屏栈     │
└────────────────────────────────────────┘   └───────────────────────────────────┘
QMT 栈（东北证券客户端 + MiniQMT/xtquant + broker/qmt* + 本地引擎 live 面）→ 分阶段退役归档
```

## 二、现状盘点（QMT 栈三层 footprint，2026-08-27 实测）

**进程层（活着的东西）**：
- `XtMiniQmt.exe`（东北证券 NET 专业版客户端，pid 31332）
- 本地引擎 live 进程树（`.venv310 python -m trading`，pid 7004/14316）
- uvicorn server（保留项：digest/pipeline/jobs——其中引擎 jobs 属交易面需处置；观测 schtasks 已收编进 lifespan，无散落计划任务）
- 掘金侧常驻：emgm3 终端 + gmterm-serv(7001-7004) + 策略进程（agent 外拉，relaunch ps1 管理）

**代码层（退役对象）**：
- `broker/qmt_connection.py / qmt_io.py / qmt.py`（QMT 网关三层）
- `trading/qmt_gateway.py / gateway_service.py / engine.py(live面) / phases/stop_loss.py / io/quotes.py`（本地引擎 QMT 耦合面；phases/pre_open.py 等的**判定逻辑已移植进 pilot**，但其带外价格 bug 未移植——随退役一并作废）
- `ops/trading_supervisor.py / process_topology.py(engine_processes) / restart_trading.py`
- `trading/tools/qmt_*smoke*.py / probe_qmt_ratelimit.py`
- `presentation/web` 实盘中控 `/live`（QMT 网关只读 UI）+ `infra` server 交易面 API
- tests：`tests/trading/test_qmt_gateway.py` 等

**数据/资产层**：
- `qmt_data/` **7.0GB**——QMT 客户端私有缓存，**全仓代码零引用**（grep 实证）→ 可整删
- `xtquant/`（SDK 库目录）+ `xtquant_250807.rar`
- 交易台账：`logs/live_trades.csv`、trading_state.db（若有）→ 归档
- 客户端安装：`E:\东北证券NET专业版(测试版)\`

**掘金腿现状（升产基线，均已验证）**：端到端成交 ✓；数据自足（日历/日线/tick 全 gm，R6-8 parity 验证）✓；五项自愈（探针/启动回补/死单回补/竞态防护/钳涨停带）✓；运维三件套（relaunch ps1 / 7002 API / skill）✓。

## 三、差距清单（掘金腿补齐项，升产的必要增量）

| # | 差距 | 方案 | 阶段 |
|---|---|---|---|
| G-1 | **掘金终端单点**：终端挂=行情+交易+数据全停 | 看护脚本：`netstat :7001` 探测 + 7002 API 心跳 → 失联即钉钉告警 + 提示人工拉起（终端启动含登录态，自动拉起列为后续实验：`goldminer://` 协议或带 `--remote-debugging-port` 的看护进程） | P0 |
| G-2 | 台账仅 audit CSV + state.pkl | **W3 采集器提前**：`audit_*.csv → experiments.db`（account_id 维度表已预留），晨检一条 SQL | P0 |
| G-3 | 晨检人工化 | Cron 自动晨检（audit INIT/探针/EOD 三查 + 7002 持仓资金对账 + 与 state.pkl 交叉），异常钉钉 | P0 |
| G-4 | 双轨对照计划（G1-G3 chase-on 重跑）依附本地腿 | **正式取消**——替代口径：D1-D6 分歧台账已在 pilot audit 留痕（tp1_eod_sweep 等标记），单轨自身可审计 | P1 文档化 |
| G-5 | cockpit `/live` 观测页绑 QMT 网关 | 改接 7002 API（positions/cash/orders 全有）或直接退役 `/live`（skill 查询已覆盖）→ 裁决点② | P1 |
| G-6 | server 引擎 jobs（pre_open cron 09:22 等） | 随本地引擎停用下线（自动参数优化已停的前例 fd0d2823 同型操作）；server 保留研究面（digest/pipeline/发现） | P1 |
| G-7 | chase_entry（D1）两侧均无实盘实现 | 维持现状（回测口径一致）；未来若补，只在掘金腿补 | 不阻塞 |

## 四、分阶段退役方案

### Phase 0 · 升产基线（**2026-08-27 已启动**，观察期至 2026-09-03）
掘金腿已达标，本阶段只加看护与台账，不动 QMT。
1. ✅ G-1 看护（ops/gm_terminal_guard.py + QuanterGmGuard schtasks 5min；含策略进程自愈 relaunch，终端只告警不拉起）
2. ✅ G-2 采集器（ops/emquant_audit_ingest.py + 15:40 schtasks；terminal_audit 表，首采 2900 行）
3. ✅ G-3 晨检（ops/emquant_morning_check.py + 09:40 schtasks；六查含账实对账，首跑全绿）
3. **验收 KPI（连续 5 交易日）**：INIT/半点探针/EOD 全勤；零人工干预（无手动重启/改代码）；日终对账 API↔state.pkl 持仓资金零漂移；audit 无未解释的 WARN 洪泛
4. 本地引擎照旧冷跑（其 QMT 腿零成交已实锤，风险敞口=零）

### Phase 1 · 本地引擎降级（✅ 2026-08-27 执行，用户指令提前——观察期 KPI 由用户裁决放弃）
1. 停 `-m trading` 进程树 + `ops/trading_supervisor`
2. server：下线引擎 jobs 与交易面 API（研究面保留）；cockpit `/live` 按 G-5 裁决处置
3. L4 双轨发射器/日度对账 cron 中本地腿侧停用（掘金侧 fresh_window 观测保留）
4. **回滚 RTO < 10 分钟**：重启脚本 + QMT 客户端原样未动
5. 验收：3 个交易日掘金腿独立运转 + QMT 侧零进程零订单

### Phase 2 · 冷待机期（✅ 用户 2026-08-27 指令豁免——"p123 继续做"）
QMT 全家保持"可回滚"状态但不再启动；东北证券客户端退出不卸载。验收：用户口头确认放弃回滚权。

### Phase 3 · 拆除（✅ 2026-08-27 执行，8ff0b839）
1. `qmt_data/` 7GB 删除（代码零引用实证在案）
2. `xtquant/` + rar 归档移除；`E:\东北证券NET专业版` **用户手动卸载**（GUI 安装器，agent 不碰券商软件卸载）
3. 代码隔离：`broker/qmt*`、`trading/` QMT 面、ops supervisor 等打 `# QUARANTINED 2026-08-xx QMT 退役` 头注，整体移入 `archive/qmt-retirement-<date>/` 分支（master 保历史可考，不物理删）
4. 测试面：QMT 相关测试随代码走；`_XTQUANT_AVAILABLE` 的 try/except 退化模式天然兼容环境无 xtquant
5. 文档口径收口：README/AGENTS/记忆/引擎指纹说明（ENGINE_FILES 均为回测内核文件，不受影响）
6. 验收：全仓 grep `xtquant|qmt_gateway|XtMiniQmt` 仅命中 archive 与历史文档；pytest 全绿

## 五、回滚预案（分级）

| 级别 | 触发 | 动作 | RTO |
|---|---|---|---|
| R1 策略级 | 掘金腿故障（P0 内无法自愈） | relaunch ps1 / 修复-部署循环（已验证的秒级闭环） | 分钟 |
| R2 平台级 | 掘金终端异常超 1 交易日 | 看护告警 → 人工拉终端（goldminer-terminal skill §GUI）→ relaunch 策略 | <30 分钟 |
| R3 架构级 | 用户裁决回 QMT（P1/P2 期内） | 重启本地引擎 + trading_supervisor + server jobs（全部原样保留） | <10 分钟 |
| R4（P3 后） | 拆除后回 QMT | 从 archive 分支恢复代码 + 重装客户端 | 天级，需重验证 |

## 六、待用户裁决点

| # | 问题 | 默认建议 |
|---|---|---|
| ① | tushare 研究主湖是否也在"唯一数据源"范围内（即研究面也迁 gm） | **✅ 已裁决：保留 tushare**（理由同左） |
| ② | cockpit `/live` 观测页 | **✅ 已裁决：退役** |
| ③ | 东北证券客户端 | **✅ 已裁决：P3 用户手动卸载** |

## 七、风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| 掘金终端单点（登录态过期/崩溃/升级） | 高 | G-1 看护+告警；R2 预案；终端自动更新可关则关（product.json updateUrl 面向服务器，本地升级提示人工确认） |
| 掘金 API/SDK 行为漂移（版本升级） | 中 | stamp 版本锚 + 750+ 回归测试 + 拒因/审计留痕体系（今天已救场一次） |
| 仿真→未来实盘切换时的账户纪律 | 中 | C1 白名单常量制不变；实盘切换=改常量+`PILOT_ALLOW_LIVE` 知情门，独立方案另立 |
| 本地引擎知识流失（phases 判定细节） | 低 | 判定数学已移植 pilot 并有 C9 口径锚测试钉死；archive 分支可考 |
| 拆除后误删研究依赖 | 低 | qmt_data 零引用已实证；拆除前置 grep 验收门槛 |


## 八、P1-P3 执行纪要（2026-08-27 晚）

**P1（46c3895d）**：QUANTER_TRADING_FACE=off 闸入 lifespan（后随 P3 升级为结构性删除）；/live 路由重定向 /cockpit + 导航退役；ops/start_server_research_only.bat；L4 双轨对账 cron 删除（对照对象已亡）。杀旧引擎树时波及其拉起的 5 个钉钉 connect bot 子进程（随研究面 server 重启自愈）。

**P3（8ff0b839）**：
- 删除：broker/qmt×5、trading/qmt_gateway、qmt_market_data（转盲价桩模块保 import 面）、tools/qmt_*×5、ops/{miniqmt_guard,trading_supervisor,restart_trading}、server trading API 路由、前端 LiveCockpitView；测试面删 12 文件改 8 文件。
- **架构救赎（深挖发现）**：研究面四 cron（discovery/digest/autopromote）+18:00 数据管道原本全寄生在 engine.sched——引入 lifespan 自有 **ops_sched**（AsyncIOScheduler）接管；pipeline_then_eod 增 engine=None 分支跳过交易 eod 段（采集→湖修复→校验→brief→数据集同步照常）。C-8 全 job 启动补跑随引擎退役（数据侧 offline 补跑=已知遗留）。
- ops processes 端点改制（supervisor 三查退役，engine_processes 保留）。
- 资产：qmt_data 7GB + xtquant/ 已删（xtquant_250807.rar 留作备份）。
- 回归：后端 1839 绿 + 前端 32 绿；研究面 server 以 P3 架构运行中（ops_sched 日志验证）。

**遗留清单（非阻塞）**：
1. 东北证券客户端卸载——用户手动（GUI 卸载器）。
2. QuanterServer 开机自启——schtasks 注册被拒（需管理员 PowerShell 一次性注册 ops/start_server_research_only.bat，或开机手动跑）。
3. cockpit 交易卡（StatusCard/AssetCard/TradesTable）仍轮询已删路由得 404 降级——前端专项清理待做（api/trading.ts 已加墓碑注记）。
4. 重启跨 18:00 的数据链补跑缺口——次日 pipeline 自然接续或手动 `python -m ops.data_pipeline`。
5. trading/ 引擎核心（engine/phases/order_state/calendar/critical）保留为研究依赖（emquant/export_snapshot 链、backtest mock、run_data_check），dormant 态不再装配。
