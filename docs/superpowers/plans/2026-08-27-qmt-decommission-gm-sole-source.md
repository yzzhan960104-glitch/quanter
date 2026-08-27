# QMT/东北证券全面退役方案——东财掘金升格唯一实盘平台与数据源

> 日期：2026-08-27 ｜ 状态：待用户批准
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

### Phase 0 · 升产基线（现在 → 连续 5 个交易日观察期）
掘金腿已达标，本阶段只加看护与台账，不动 QMT。
1. G-1 看护脚本 + G-3 晨检 cron 上线（agent 实施）
2. G-2 采集器上线（agent 实施）
3. **验收 KPI（连续 5 交易日）**：INIT/半点探针/EOD 全勤；零人工干预（无手动重启/改代码）；日终对账 API↔state.pkl 持仓资金零漂移；audit 无未解释的 WARN 洪泛
4. 本地引擎照旧冷跑（其 QMT 腿零成交已实锤，风险敞口=零）

### Phase 1 · 本地引擎降级（观察期满，用户点头）
1. 停 `-m trading` 进程树 + `ops/trading_supervisor`
2. server：下线引擎 jobs 与交易面 API（研究面保留）；cockpit `/live` 按 G-5 裁决处置
3. L4 双轨发射器/日度对账 cron 中本地腿侧停用（掘金侧 fresh_window 观测保留）
4. **回滚 RTO < 10 分钟**：重启脚本 + QMT 客户端原样未动
5. 验收：3 个交易日掘金腿独立运转 + QMT 侧零进程零订单

### Phase 2 · 冷待机期（1-2 周）
QMT 全家保持"可回滚"状态但不再启动；东北证券客户端退出不卸载。验收：用户口头确认放弃回滚权。

### Phase 3 · 拆除
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
| ① | tushare 研究主湖是否也在"唯一数据源"范围内（即研究面也迁 gm） | **保留 tushare**：gm 拉全历史湖有限流/配额风险，研究与实盘数据源分离是特性不是债务（parity 已互证） |
| ② | cockpit `/live` 观测页 | **退役**（7002 API + skill 查询已覆盖观测需求；省一层维护） |
| ③ | 东北证券客户端 | **P3 手动卸载**（卸载器是 GUI 且涉券商软件，归用户） |

## 七、风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| 掘金终端单点（登录态过期/崩溃/升级） | 高 | G-1 看护+告警；R2 预案；终端自动更新可关则关（product.json updateUrl 面向服务器，本地升级提示人工确认） |
| 掘金 API/SDK 行为漂移（版本升级） | 中 | stamp 版本锚 + 750+ 回归测试 + 拒因/审计留痕体系（今天已救场一次） |
| 仿真→未来实盘切换时的账户纪律 | 中 | C1 白名单常量制不变；实盘切换=改常量+`PILOT_ALLOW_LIVE` 知情门，独立方案另立 |
| 本地引擎知识流失（phases 判定细节） | 低 | 判定数学已移植 pilot 并有 C9 口径锚测试钉死；archive 分支可考 |
| 拆除后误删研究依赖 | 低 | qmt_data 零引用已实证；拆除前置 grep 验收门槛 |
