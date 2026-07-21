# 钉钉观测运营层 + 后台综合看板 设计（第一期）

- **日期**：2026-07-21
- **状态**：设计已与研究员对齐，待出实施计划
- **范围**：第一期「观测运营层」（今天做完、明天上线）；第二期「自动交易引擎」本周另行立 spec

---

## 1. 背景与目标

### 1.1 当前进展
1. 颈线法形态学策略已就绪（全市场凯利年化 28.4%，创板/科创口径 param_iter 最优年化 99.7%）。
2. 东北证券 miniQMT 模拟盘接入已打通（账号 `10110356`，模拟资金 100 万，connect/资产/持仓/报单/撤单指令/主动查询全通；盘后撤单不生效属交易时段特性）。

### 1.2 目标
把项目从「能回测 + 能手工下单」推进到「**钉钉每日可见 + 后台可查明细**」的运营态，为第二期无人值守自动交易铺好观测与可观测性地基。

### 1.3 核心约束（探索定论）
- **自动交易引擎明天上不了**：颈线法策略零实盘 caller、无 scheduler、海龟止损锁在回测 `simulate_exit`、盘后对账无人调度、`.env` 白名单只有 4 只 ETF 不覆盖颈线法股票池。赶工 = 断链/止损缺失/敞口失控的雷。第二期本周交付。
- **第一期严格只读**：播报 + 查询，不下单。零资金风险。

---

## 2. 范围

| | 第一期（本 spec） | 第二期（另行 spec） |
|---|---|---|
| **钉钉** | 3 个专业机器人（交易/数据/策略）定时播报 + @查询；复用通用机器人作 CLI 大脑 | — |
| **后台** | `/cockpit` 综合看板：交易流水查询 + 实时日志 + 历史回测对比 | — |
| **交易引擎** | 不动（模拟盘保持手工/脚本下单） | scheduler + live 信号生成器 + 海龟止损迁出 + 盘后对账 |

---

## 3. 整体架构

```
┌──────────────────────── 钉钉侧（身份 / 群）────────────────────────┐
│  群：yzzhan量化（BROADCAST_GROUP_ID=ciduznBwLLiWKcMewBOF4+kWQ==）   │
│                                                                    │
│  ① 通用机器人 yzzhanCli          【已有，CLI 完整能力大脑】          │
│     = Claude Code agent          @万能问答/改代码/跑命令/落盘        │
│            ▲                                                       │
│            │ 转发通用大脑（dws --channel claudecode，零代码）       │
│  ② 交易机器人  ③ 数据机器人  ④ 策略微机器人   【新建】              │
│     盘后定时播报 + @查询（转发①，专业身份回复）                     │
└────────────────────────────┬───────────────────────────────────────┘
                             │ dws dev connect（入站@）/ send-by-bot（出站播报）
                             ▼
┌──────────────────────── 项目侧 quanter ───────────────────────────┐
│  broadcast/    播报生成（复用 push.py 出站 + 新增 3 个 brief）      │
│  server/       FastAPI 数据源（多数 API 已就绪，补 1 个流水查询）   │
│  logs/         live_trades.csv / param_iter_state.json → 播报+看板  │
└────────────────────────────┬───────────────────────────────────────┘
                             ▼
┌──────────────────────── 后台看板 web/ ────────────────────────────┐
│  新增 /cockpit：流水查询 + 实时日志 + 历史回测对比（复用为主）      │
└───────────────────────────────────────────────────────────────────┘
```

**核心设计洞察**：
1. **@查询零开发**——三个专业机器人各自 `dws dev connect --channel claudecode`，驱动同一个 Claude Code（共享 workdir + 项目 API），回复自带专业身份。不重复造 agent。
2. **定时播报复用 `broadcast/` 模板**——`brief.py`（纯函数生成 Markdown）+ `push.py`（dws send-by-bot 出站）+ `__main__.py`（CLI 入口 + 幂等去重）已验证，三个新机器人各加一个 brief 模块即可。
3. **群复用**——全部进 `yzzhan量化` 群，集中运营、方便对照。

---

## 4. 组件设计

### 4.1 通用机器人 ①（复用，不改）
- 现状：`yzzhanCli` 统一应用（unified-app-id `f0b2740f-…`），`dws dev connect --channel claudecode --agent-memory --agent-approval-mode ask`，已是全功能 CLI agent。
- 角色：三个专业机器人的「大脑」+ 兜底万能问答/改代码/跑命令。审批闸 `ask` 保留（改代码/跑命令需人工确认）。

### 4.2 三个专业机器人（新建）

每个机器人两件事：**定时播报**（B 类出站）+ **@查询转发**（驱动 Claude Code）。

#### ② 交易机器人
- **定时播报**（默认 15:30，可配置）：当日挂单/撤单/成交笔数与明细、**期初资金 → 期末资金**、当日盈亏、收盘持仓快照。
  - 数据源：`logs/live_trades.csv`（成交流水）+ `trading_service.query_stock_asset`（资金）+ `GET /trading/orders`（订单回报）+ `GET /trading/positions`（持仓）。
  - **诚实边界**：「止盈止损」字段第一期**留位占位**（如实标注「第二期交易引擎上线后填充」），不播不存在的止损动作，绝不造假数据。
- **@查询**（驱动 Claude Code 大脑，专业身份回复）：查持仓/流水/资产/撤单状态。
- 钉钉应用：新建统一应用，unified-app-id 待建号后填 `.env`。

#### ③ 数据机器人
- **定时播报**（默认 17:00，可配置）：35 数据集健康度统计（healthy/stale/missing/failed 计数）、最老数据 lag 天数、当日同步动作汇总。
  - 数据源：`server/services/data_service._derive_status()`（5 态推导，已就绪）+ `logs/` 同步日志。
- **@查询**（驱动 Claude Code 大脑，专业身份回复）：查某数据集状态/触发同步。
- 钉钉应用：新建统一应用。

#### ④ 策略微机器人
- **定时播报**（默认 16:00，可配置）：颈线法当日扫描信号数、参数迭代状态（`logs/param_iter_state.json`）、近期回测胜率/最大回撤/年化。
  - 数据源：`caisen/facade.py` 扫描接口 + `logs/param_iter_state.json` + `replay_runs/index.json`（历史回测）。
- **@查询**（驱动 Claude Code 大脑，专业身份回复）：查当日信号/跑回测/查参数。
- 钉钉应用：新建统一应用。

### 4.3 后台综合看板 `/cockpit`（第一期）

新增路由 `/cockpit`，聚合小部件。**复用为主，只补 3 块新前端页**（后端 API 多数已就绪）：

| 看板块 | 后端 | 前端 | 状态 |
|---|---|---|---|
| 实盘心跳 + 资金卡 | `GET /trading/status` `/asset` | 复用 `/live` 组件 | ✅ 复用 |
| 数据湖健康度 | `GET /data/datasets` | 复用 `/data` 组件 | ✅ 复用 |
| **交易流水查询** | `GET /trading/trades`（**新增**，分页读 `live_trades.csv`） | **新增** 明细表（日期/标的/方向筛选 + 状态徽章） | 🆕 新增 |
| **实时日志** | `GET /logs/stream` SSE（**已就绪**） | **新增** TerminalLogs 组件订阅 EventSource | 🆕 前端新增 |
| **历史回测对比** | `GET /caisen/replay/runs` + `/caisen/replay/tasks`（**已就绪**） | **新增** 对比页（多 run 资金曲线叠加 + 统计差异表） | 🆕 前端新增 |

---

## 5. 数据流

### 5.1 定时播报数据流
```
schtasks 定时触发 → run_<bot>_brief.bat（cd 项目根，解决 cwd=System32 坑）
  → python -m broadcast --bot <trading|data|strategy>
    → brief_<bot>.py 纯函数：读数据源（CSV/API/JSON）→ 生成 Markdown
    → push.py：dws send-by-bot --robot-code <BOT_ROBOT_CODE> --group <GROUP_ID>
    → 写 logs/.last_<bot>_brief 幂等去重（防 schtasks 重跑重复推送）
```

### 5.2 @查询数据流
```
钉钉 @专业机器人 → dws dev connect（常驻，--channel claudecode）
  → 驱动 Claude Code（共享 workdir=项目根，--agent-approval-mode ask）
  → Claude Code 调项目 API / 读文件 / 跑命令 回答
  → dws 以专业机器人身份把回复推回钉钉
```

### 5.3 看板数据流
```
浏览器 → /cockpit → 各小部件调对应 API（流水查询/日志SSE/回测对比/心跳/数据健康）
  → 后端读 live_trades.csv / SSE 环缓冲 / replay_runs JSON / 实时网关状态
```

---

## 6. 配置化设计（播报时间 / 群 / 应用）

**原则**：所有可变参数走 `.env`，不硬编码。改时间 = 改 `.env` + 重跑 schtasks 管理脚本。

### 6.1 `.env` 新增
```ini
# === 观测运营层：三个专业机器人 ===
# 群：复用既有 BROADCAST_GROUP_ID（yzzhan量化群），不新增群变量

# ② 交易机器人（盘后播报 + @查询）
TRADING_BOT_UNIFIED_APP_ID=<建号后填>
TRADING_BOT_ROBOT_CODE=<建号后填>
TRADING_BOT_ALLOWED_STAFF_IDS=${DINGTALK_ALLOWED_STAFF_IDS}
TRADING_BRIEF_TIME=15:30

# ③ 数据机器人
DATA_BOT_UNIFIED_APP_ID=<建号后填>
DATA_BOT_ROBOT_CODE=<建号后填>
DATA_BOT_ALLOWED_STAFF_IDS=${DINGTALK_ALLOWED_STAFF_IDS}
DATA_BRIEF_TIME=17:00

# ④ 策略微机器人
STRATEGY_BOT_UNIFIED_APP_ID=<建号后填>
STRATEGY_BOT_ROBOT_CODE=<建号后填>
STRATEGY_BOT_ALLOWED_STAFF_IDS=${DINGTALK_ALLOWED_STAFF_IDS}
STRATEGY_BRIEF_TIME=16:00
```

### 6.2 schtasks 管理脚本（配置化注册）
新增 `scripts/manage_ops_schtasks.py`：
- 读 `.env` 的 `*_BRIEF_TIME`，幂等注册 3 个 schtasks（`QuanterTradingBrief` / `QuanterStrategyBrief` / `QuanterDataBrief`），各指向 `scripts/run_<bot>_brief.bat`。
- 支持 `--list` / `--register` / `--unregister` / `--rerun <bot>`。
- 改时间：改 `.env` → `python manage_ops_schtasks.py --register`（脚本先删后建，幂等）。
- **第二期统一调度**：交易引擎引入 APScheduler 后，播报调度迁移到 APScheduler 进程内（改配置 reload 即可，免 schtasks 重建），本脚本届时退役或保留作 fallback。

---

## 7. 错误处理（边界审查）

| 场景 | 处理 |
|---|---|
| dws send-by-bot 出站失败（errcode≠0/网络） | `push.py` 已捕获 + 重试 3 次 + 失败写 `logs/<bot>_push.fail`；不抛异常打断播报 |
| 数据源缺失（如 `live_trades.csv` 当日空） | brief 纯函数返回「当日无交易」中性文案，不报错不推送空数据 |
| schtasks 重跑（系统补执行） | 幂等去重（`logs/.last_<bot>_brief` 记上次推送日期），同日不重复推 |
| @查询转发 Claude Code 超时 | dws `BRIDGE_ASK_TIMEOUT=120` 已有；专业机器人沿用，超时回「查询超时，请到后台 /cockpit 查看明细」 |
| Claude Code 改代码/跑命令 | `--agent-approval-mode ask` 审批闸，人工确认才执行（防误操作） |
| 模拟盘网关断线 | 交易 brief 读取前探测 `GET /trading/status`，断线时如实标注「网关断线，数据可能不全」 |

---

## 8. 测试策略

- **brief 纯函数单测**：`tests/broadcast/test_brief_trading.py` / `test_brief_data.py` / `test_brief_strategy.py`——mock CSV/API/JSON 数据源，断言生成的 Markdown 含关键字段、无 NaN、空数据降级文案正确。
- **流水查询 API 单测**：`tests/server/test_trading_trades.py`——构造 `live_trades.csv` 样本，测分页/筛选/边界（空文件、跨日）。
- **幂等去重测试**：同日二次触发 `python -m broadcast --bot trading` 不重复推送。
- **看板前端**：`web` 用 vitest + @vue/test-utils 测流水表筛选、日志 SSE 订阅 mock、回测对比页渲染。
- **端到端冒烟**（明天上线前）：手动触发 3 个 brief → 确认钉钉群收到 3 份报告；打开 `/cockpit` 确认 5 块小部件渲染。

---

## 9. 时间线

| 时间 | 交付 |
|---|---|
| **今天（7-21）** | 3 个 brief 模块 + 单测；钉钉建 3 个统一应用、拉群、send-by-bot 验证；`/cockpit` 看板（流水查询 API+页、实时日志页、回测对比页）；`manage_ops_schtasks.py`；端到端冒烟 |
| **明天（7-22）** | 注册 3 个 schtasks + 起 3 个 dws dev connect 常驻 → **观测层正式上线**；模拟盘保持手工/脚本可下单 |
| **本周二期** | 自动交易引擎 spec + 实施：scheduler + live 信号生成器 + 海龟止损迁出 + 盘后对账调度 |

---

## 10. 风控边界（红线）

1. **第一期严格只读**：播报 + 查询，任何组件不调 `submit_order`/`cancel_order`。模拟盘不自动下单。
2. **@查询审批闸**：转发 Claude Code 的 `--agent-approval-mode ask` 必须保留；改代码/跑命令/落盘需人工确认。
3. **诚实播报**：交易 brief 的止盈止损字段第二期才有，第一期如实占位，不造假。
4. **凭证安全**：所有 `*_UNIFIED_APP_ID`/`*_ROBOT_CODE` 仅 `.env`（已 gitignore），不进 commit。
5. **幂等护栏**：每个播报机器人 `logs/.last_<bot>_brief` 去重，防 schtasks 补执行重复推送扰民。

---

## 11. 现有设施复用清单 vs 缺口清单

### 复用（直接拿来用）
- `broadcast/push.py`（dws send-by-bot 出站）、`broadcast/__main__.py`（CLI + 幂等）、`broadcast/name_resolver.py`
- `server/api/v1/trading.py`（/status /asset /positions /orders /export）、`data.py`（/datasets）、`caisen.py`（/scan /replay/runs /replay/tasks）、`logs.py`（/stream SSE）
- 通用机器人 `yzzhanCli`（CLI 大脑）
- `yzzhan量化` 群（`BROADCAST_GROUP_ID`）
- dws 建号/拉群/send-by-bot/dev connect 全套 SOP（`scripts/setup_broadcast_bot.md` 等）

### 新增（第一期要开发）
- `broadcast/brief_trading.py` / `brief_data.py` / `brief_strategy.py` + 单测
- `scripts/run_trading_brief.bat` / `run_data_brief.bat` / `run_strategy_brief.bat`
- `scripts/manage_ops_schtasks.py`（配置化 schtasks 管理）
- 钉钉侧：3 个统一应用（建号 + 拉群 + send-by-bot 验证）
- 后端：`GET /api/v1/trading/trades`（流水分页查询，读 CSV）
- 前端：`web/src/views/CockpitView.vue` + 三个子组件（流水表 / TerminalLogs / 回测对比）+ 路由 + 顶栏入口
- `scripts/start_dingtalk_bots.md` 更新（加 3 个专业机器人常驻 SOP）

### 第二期（不在本 spec，仅备忘）
- `trading_calendar.py` + APScheduler 统一调度
- live 信号生成器（`scan_at` → `submit_order`）
- 海龟止损迁出 `simulate_exit` → 实盘持仓监控
- 盘后对账调度（`reconcile()` + 偏差告警）
- `.env` 开 `QMT_ALLOW_LIVE_TRADE=true` + 白名单扩颈线法股票池
