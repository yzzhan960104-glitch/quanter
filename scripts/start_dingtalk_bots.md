# 钉钉机器人启动（dws 统一接入）

> 2026-07-16 迁移：bridge 自研（dingtalk-stream + ClaudePool + Alarmer）退役，两个机器人都走 **dws dev connect**，一套接入逻辑。
> 2026-07-21 一期观测运营层：新增 3 专业机器人（交易/数据/策略），各自播报 + @查询常驻。上线流程见下方「观测层上线 SOP」。

## 前置
1. **dws 登录**：`dws auth login`（浏览器授权，见 [dingtalk-workspace-cli](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli)）。凭证持久化，30 天有效。
2. **.env 凭证**（已配）：`GLM_API_KEY`（z.ai coding plan）/ `REVIEW_*`（审查 webhook 推报告）/ `DINGTALK_*`（备用）。
3. **Python**：`.venv310/Scripts/python.exe`（Python 3.10）。

---

# 一、既有常驻进程（通用对话 + training loop 审查 + uvicorn 服务）

## 启动三个常驻进程

### 1. bridge 对话机器人（yzzhanCli通用）
```bash
dws dev connect --unified-app-id f0b2740f-c029-4b99-943c-58de139c7463 \
  --channel claudecode --agent-memory --agent-approval-mode ask \
  --allowed-users <DINGTALK_ALLOWED_STAFF_IDS> \
  --agent-workdir <项目根，如 C:/Users/yzzhan/Desktop/quanter>
```
- @yzzhanCli通用 → dws 收 → Claude Code 对话（--agent-memory 续聊）
- --allowed-users：身份闸（.env 里 `DINGTALK_ALLOWED_STAFF_IDS` 白名单 staff_id，承接老 bridge safety 白名单 → dws 身份闸；省略 = 任何 @该机器人的钉钉用户都能驱动本机 Claude Code，bypassPermissions 全放行下属高危，必填）
- --agent-approval-mode ask：审批闸（事前确认，替代老 Alarmer 事后告警）
- --agent-workdir：Claude Code 工作目录设项目根（读 CLAUDE.md/memory/代码；省略则跑空白 Temp 目录、不了解项目做不了开发，已踩坑）

### 2. 审查训练机器人（yzzhan参数优化）
```bash
dws dev connect --unified-app-id e2695383-6fe9-4617-9439-2a8538af3107 \
  --channel custom --agent-cmd "C:/Users/yzzhan/Desktop/quanter/.venv310/Scripts/python.exe C:/Users/yzzhan/Desktop/quanter/scripts/dingtalk_review_bridge.py" \
  --allowed-users <DINGTALK_ALLOWED_STAFF_IDS>
```
- @yzzhan参数优化 → dws 收 → bridge脚本 → `POST /api/v1/training/review` → orchestrator.submit_review（training loop 人审关卡）
- **绝对路径**（dws cwd 不是项目根，相对路径找不到 python.exe，已踩坑）
- --allowed-users：身份闸（限制谁能 @触发 training loop；Task4 删 ReviewChatbotHandler 白名单后 dws 层补，省略 = 任何人 @都能触发训练消耗算力）

### 3. uvicorn 服务（training loop + webhook 推 + /review 端点 + 观测层 API）
```bash
C:/Users/yzzhan/Desktop/quanter/.venv310/Scripts/python.exe -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```
- lifespan 装 TrainingLoopOrchestrator（daemon）+ replay_scheduler
- DingTalkNotifier（webhook 推报告/回显，urllib，不用 dingtalk-stream SDK）
- 一期观测层 API（`/trades` `/data/datasets` SSE 日志 等）也挂此 app；前端 `/cockpit` 看板依赖它。

---

# 二、观测层上线 SOP（一期：3 专业机器人 · 用户照抄执行）

> ⚠️ **范围声明**：以下步骤包含**不可撤销的外向动作**（建真实钉钉资源 / 群发消息 / 系统任务 / 常驻进程），由用户在真实环境按本 SOP 依次执行；AI 助手/代码改动只负责把命令、脚本、占位准备好，不替用户按下这些动作。
> 已 dry-run 验证：3 份文案生成正常、`manage_ops_schtasks.py --list` 脚本可跑（详见 `task-13-ops-deployment-report.md`）。

## 上线执行清单（按序）

### Step 1 · 建 3 个 dws 应用机器人（异步 · 拿 robotCode）

群统一复用 `yzzhan量化`（`ciduznBwLLiWKcMewBOF4+kWQ==`，`BROADCAST_GROUP_ID`），不新建群。

```bash
# 交易机器人
dws dev app robot submit --name "quanter交易机器人" --robot-name "quanter交易" --desc "每日交易跟踪播报" -y
# 数据机器人
dws dev app robot submit --name "quanter数据机器人" --robot-name "quanter数据" --desc "每日数据健康度播报" -y
# 策略机器人
dws dev app robot submit --name "quanter策略机器人" --robot-name "quanter策略" --desc "每日策略健康度播报" -y
```

> ⚠️ **desc 坑（errorCode 67010）**：描述只能含中文 / 英文字母 / 数字 / 指定标点，**不能含 `/`、`:`** 等。本 SOP 三条 desc 均已用纯文字，照抄即可。
> 提交是异步的：返回 `taskId` → 轮询 `dws dev app robot result --task-id <taskId> -y` 到 `SUCCESS` 拿 `robotCode`；失败带 `--task-id <原 taskId>` 复用重试（不生成新任务）。

记下 3 个 `robotCode` → Step 4 写 `.env`：
- `TRADING_BOT_ROBOT_CODE=<返回值>`
- `DATA_BOT_ROBOT_CODE=<返回值>`
- `STRATEGY_BOT_ROBOT_CODE=<返回值>`

同时记下 3 个机器人各自的 **统一应用 ID**（`unifiedAppId`，开放平台控制台「应用信息」或 `dws dev app list` 可查）→ Step 6 常驻 connect 要用。

### Step 2 · 拉进 yzzhan量化群（每个机器人一次）

```bash
dws chat group members add-bot --robot-code <TRADING_BOT_ROBOT_CODE>  --id ciduznBwLLiWKcMewBOF4+kWQ== -y
dws chat group members add-bot --robot-code <DATA_BOT_ROBOT_CODE>     --id ciduznBwLLiWKcMewBOF4+kWQ== -y
dws chat group members add-bot --robot-code <STRATEGY_BOT_ROBOT_CODE> --id ciduznBwLLiWKcMewBOF4+kWQ== -y
```

幂等检查（重复部署跳过）：`dws chat group bots --id ciduznBwLLiWKcMewBOF4+kWQ==` 查到对应 robotCode → 跳过。

### Step 3 · 出站通道验证（每机器人真发一条测试，确认 OAuth + robotCode + 群 全链路通）

```bash
dws chat message send-by-bot --robot-code <TRADING_BOT_ROBOT_CODE>  --group ciduznBwLLiWKcMewBOF4+kWQ== --title 测试 --text "交易机器人连通测试" -y
dws chat message send-by-bot --robot-code <DATA_BOT_ROBOT_CODE>     --group ciduznBwLLiWKcMewBOF4+kWQ== --title 测试 --text "数据机器人连通测试" -y
dws chat message send-by-bot --robot-code <STRATEGY_BOT_ROBOT_CODE> --group ciduznBwLLiWKcMewBOF4+kWQ== --title 测试 --text "策略机器人连通测试" -y
```
`success=true` 即通道 OK，群应收到 3 条测试。

### Step 4 · 回填 `.env`（本地真值，绝不进 git）

```dotenv
TRADING_BOT_ROBOT_CODE=<Step 1>
DATA_BOT_ROBOT_CODE=<Step 1>
STRATEGY_BOT_ROBOT_CODE=<Step 1>
TRADING_BOT_UNIFIED_APP_ID=<Step 1 末尾查的 unifiedAppId>
DATA_BOT_UNIFIED_APP_ID=<Step 1 末尾查的 unifiedAppId>
STRATEGY_BOT_UNIFIED_APP_ID=<Step 1 末尾查的 unifiedAppId>
BROADCAST_GROUP_ID=ciduznBwLLiWKcMewBOF4+kWQ==
# 触发时间（默认已在 .env.example 给出，按需改）
TRADING_BRIEF_TIME=15:30
STRATEGY_BRIEF_TIME=16:00
DATA_BRIEF_TIME=17:00
```

### Step 5 · dry-run 三份文案（不发钉钉，只打印；改完 .env 先验文案）

```bash
cd C:/Users/yzzhan/Desktop/quanter
.venv310/Scripts/python.exe -m broadcast --bot trading  --dry-run
.venv310/Scripts/python.exe -m broadcast --bot data     --dry-run
.venv310/Scripts/python.exe -m broadcast --bot strategy --dry-run
```
确认 3 份文案正常（成交/资金/持仓 / 数据集健康分 / 信号+参数迭代+回测）。

### Step 6 · 起 3 个 @查询常驻（专业机器人 = Claude Code 大脑转发）

每个专业机器人一个常驻进程（单独终端窗口/Windows Terminal tab/PM2 任选），照抄：

```bash
# 交易 @查询
dws dev connect --unified-app-id <TRADING_BOT_UNIFIED_APP_ID> \
  --channel claudecode --agent-memory --agent-approval-mode ask \
  --allowed-users <DINGTALK_ALLOWED_STAFF_IDS> \
  --agent-workdir C:/Users/yzzhan/Desktop/quanter

# 数据 @查询
dws dev connect --unified-app-id <DATA_BOT_UNIFIED_APP_ID> \
  --channel claudecode --agent-memory --agent-approval-mode ask \
  --allowed-users <DINGTALK_ALLOWED_STAFF_IDS> \
  --agent-workdir C:/Users/yzzhan/Desktop/quanter

# 策略 @查询
dws dev connect --unified-app-id <STRATEGY_BOT_UNIFIED_APP_ID> \
  --channel claudecode --agent-memory --agent-approval-mode ask \
  --allowed-users <DINGTALK_ALLOWED_STAFF_IDS> \
  --agent-workdir C:/Users/yzzhan/Desktop/quanter
```
- @quanter交易 / @quanter数据 / @quanter策略 → dws 收 → Claude Code 专业问答（隔离 yzzhanCli 通用对话）
- `--allowed-users` / `--agent-approval-mode ask` / `--agent-workdir` 三参数含义同上文「bridge 对话机器人」，身份闸 / 审批闸 / 工作目录缺一不可。

### Step 7 · 注册 3 个播报 schtasks（每日定时触发）

```bash
cd C:/Users/yzzhan/Desktop/quanter
.venv310/Scripts/python.exe scripts/manage_ops_schtasks.py --register
.venv310/Scripts/python.exe scripts/manage_ops_schtasks.py --list
```
- `--register` 先 `/Delete` 再 `/Create`，**幂等**（改时间 = 改 `.env` + 重跑 `--register`）。
- `--list` 看到 3 个任务 `QuanterTradingBrief` / `QuanterStrategyBrief` / `QuanterDataBrief` 即注册成功。
- 改时间：改 `.env` 的 `*_BRIEF_TIME` → 重跑 `--register`（先删后建覆盖）。
- 卸载：`manage_ops_schtasks.py --unregister`。

### Step 8 · 冒烟真发（每机器人真发一份当日报告）

```bash
cd C:/Users/yzzhan/Desktop/quanter
.venv310/Scripts/python.exe -m broadcast --bot trading  --force
.venv310/Scripts/python.exe -m broadcast --bot data     --force
.venv310/Scripts/python.exe -m broadcast --bot strategy --force
```
- `--force` 忽略幂等去重（首次上线用，确保不因已发而跳过）。
- 或立即手动触发 schtasks：`.venv310/Scripts/python.exe scripts/manage_ops_schtasks.py --rerun trading`（data/strategy 同理）。
- 群应收到 3 份当日播报；无报错即上线完成。

## 上线后常驻进程清单（一期 · 共 6 个）

| 进程 | 职责 | 启动命令见 |
|------|------|-----------|
| uvicorn server.main:app (127.0.0.1:8000) | training loop + webhook + 观测层 API（`/trades` `/data/datasets` SSE 等） | 「一、3」 |
| `yzzhanCli通用` 常驻 | 通用 Claude Code 对话 | 「一、1」 |
| `yzzhan参数优化` 常驻 | training loop 人审桥 | 「一、2」 |
| `quanter交易` 常驻 | 交易专业 @查询 | 「二、Step 6」 |
| `quanter数据` 常驻 | 数据专业 @查询 | 「二、Step 6」 |
| `quanter策略` 常驻 | 策略专业 @查询 | 「二、Step 6」 |

定时任务（非常驻，到点跑完即退）：`QuanterTradingBrief` @ 15:30 / `QuanterStrategyBrief` @ 16:00 / `QuanterDataBrief` @ 17:00（时间从 `.env` 读，幂等重建）。

---

## 职责隔离（多机器人多职责）
每个机器人 = 一个统一应用 + 一个 dws dev connect，职责由 `--channel`/`--agent-cmd` 定：
- `--channel claudecode` = 对话职责（@→Claude Code）
- `--channel custom --agent-cmd <脚本>` = 任意业务职责（@消息作为 argv 喂脚本）
- 加新职责 = 建新统一应用机器人 + 写 agent-cmd 脚本 + 起 dws dev connect

播报（`python -m broadcast --bot <X>`）不走常驻：schtasks 到点触发 → bat → python -m broadcast → push_brief → `dws chat message send-by-bot` 出站，跑完即退。

## 退役说明
- `bridge/`（自研 dingtalk-stream + ClaudePool + Alarmer）已删（commit 679d731）
- `dingtalk-stream` SDK 已从 requirements.txt 移除（两机器人都走 dws，无人用）
- `DingTalkNotifier`（webhook 推报告）保留 —— urllib，不走 SDK

---

# 四、自动交易引擎常驻（二期：影子模式 SOP · live 切换硬闸）

> ⚠️ **范围声明**：以下步骤涉及**真实下单通道的切换**（不可逆外向动作）。本 SOP 只准备 bat / 文档 / 冒烟脚本；**实际切 live、注册 schtasks、真实行情源接线**由用户在真实环境按序执行，AI 助手/代码改动不替用户按下这些动作。
> 影子冒烟已验证（Task 11）：eod_plan 落盘 plan_<today>.json + push_plan_to_dingtalk monkeypatch 不真发 dws（详见 `task-11-report-trading.md`）。

## 影子模式启动（dry_run，默认）

### Step 1 · 确认 `.env` 影子模式开关

```dotenv
# 默认 dry_run（缺省即 dry_run，宁可漏挂也不在未观测足够天数时盲发真单）
AUTO_TRADE_MODE=dry_run
TRADE_SHADOW_MIN_DAYS=5           # 影子最小观测天数（硬闸，切 live 前必须跑满）
TRADE_PLAN_DIR=logs/trading_plans # 计划落盘目录（cwd 相对路径，依赖 bat 的 cd /d 锁根）
TRADE_CAPITAL=1000000             # 仓位 cap 基准（pos_cap 计算用）
# 四 cron 时点（缺省值对齐 A 股交易日历，Task4 已配）
ENGINE_EOD_PLAN_CRON=35 15 * * 1-5   # T-1 晚扫信号 + 落计划 + 推钉钉
ENGINE_PRE_OPEN_CRON=22 9 * * 1-5     # T 日开盘前撤昨日 + 挂当日单
ENGINE_STOPLOSS_CRON=*/5 9-14 * * 1-5 # 盘中每 5 分钟止损监控
ENGINE_POST_CLOSE_CRON=30 15 * * 1-5  # 盘后对账 + 清白名单
```

### Step 2 · 启动 engine 进程（三选一）

**方式 A · Terminal tab 手动挂（开发 / 初期影子观测推荐）**

双击 `scripts/run_trading_engine.bat` 或在 Windows Terminal 开 tab 跑：

```
scripts\run_trading_engine.bat
```

**方式 B · PM2 托管**

```bash
pm2 start scripts/run_trading_engine.bat --name trading-engine
pm2 logs trading-engine
```

**方式 C · schtasks 开机自启（影子稳定后切生产推荐）**

```bash
# 一次性注册（幂等：删后建覆盖；改时点 = 改 .env + 重跑 bat 不变）
schtasks /Create /TN "QuanterTradingEngine" /TR "C:\Users\yzzhan\Desktop\quanter\scripts\run_trading_engine.bat" /SC ONSTART /RU "<Windows用户名>" /RP "<密码>" /F
schtasks /Query /TN "QuanterTradingEngine" /V
# 立即触发一次（不等开机）
schtasks /Run /TN "QuanterTradingEngine"
# 卸载
schtasks /Delete /TN "QuanterTradingEngine" /F
```

> `run_trading_engine.bat` 内部已固定 `chcp 65001 + PYTHONIOENCODING=utf-8 + PYTHONUTF8=1`（防 schtasks/Git Bash 中文日志乱码——Task10 M4 冒烟发现）。

### Step 3 · 影子观测验证清单（每日核对）

启动后每天（按四 cron 触发）核对以下信号，确认影子模式正常：

| 时点 | 预期产物 | 核对方式 |
|------|---------|---------|
| 15:35 `eod_plan` | `logs/trading_plans/plan_<T>.json` 落盘（confirmed=False）+ 交易机器人群收到 T-1 计划 Markdown | 查文件 + 钉钉群看推送 |
| 09:22 `pre_open` | 日志 `pre_open 完成 ... mode=dry_run submitted=0/N`（**DRY_RUN 不真单**） | 查 stdout / pm2 logs |
| 9-14 每 5min `stop_loss` | 日志 `stop_loss_monitor 完成 ... mode=dry_run`（stop_prices 当前 None → no-op「无止损价配置」） | 查 stdout（二期 live 前必修项，见下） |
| 15:30 `post_close` | 日志 `post_close 完成 ... drift=...`（gw=None 时跳过对账，清白名单 OK） | 查 stdout |

**dry_run 流水硬约束**：`pre_open` 挂单日志中所有 `state=DRY_RUN`（不真触达网关），`submit_order` 返 `{"order_id":"", "state":"DRY_RUN", "message":...}`。若出现 `state=FILLED` / `PARTIALLY_FILLED` 即配置错误（dry_run 没命中），立即 Ctrl-C 停 engine 排查。

## 切 live 前置条件（硬闸 · 五必须）

**五项必须全部满足才能切 live**（任一未达禁切；切 live 前请逐项打勾）：

- [ ] **① 影子跑满 `TRADE_SHADOW_MIN_DAYS`（≥5）**：完整覆盖 ≥5 个交易日，四 cron 全链路跑通无异常。
- [ ] **② 对账连续无 drift**：post_close 每日 `drift=False`，连续 ≥5 天（对账逻辑依赖 gw + local_positions，见下「二期 live 前必修」）。
- [ ] **③ 网关连通（miniQMT 模拟盘）**：`trading.get_gateway()` 返非 None，`gw._fetch_broker_positions()` 正常返持仓 dict（gw=None 时 pre_open/stop_loss/post_close 均降级 no-op，live 无法工作）。
- [ ] **④ 止损行情源已接入（`qmt_market_data.get_quote` 通道）**：miniQMT 通道可用，`get_quote(sym)["last_price"]` 返有效快照（EMT 网关无 xtdata → 止损链路需另接行情源）。
- [ ] **⑤ 二期 live 前必修三项**（当前代码 gap，未完成前禁切 live，详见下节）。

### 二期 live 前必修项（当前代码 gap · 上线集成阶段工作）

以下三项是当前 Task 1-11 显式留的 follow-up（代码 no-op / 占位），**必须在切 live 前由上线集成阶段补齐**：

1. **post_close 熔断连线**（Task 9 显式 TODO，`engine.post_close` 的 `⚠️ follow-up` 段）：
   - 当前 `post_close` 不做熔断（只对账 + 清白名单）。
   - 必修：定义 equity 数据源（如 `gw.query_asset` 或新增 `trading_service.get_equity` 接口），串联三步：
     - `circuit_breaker.check_daily_loss_limit(start_equity, curr_equity)` → True 即触发熔断
     - `circuit_breaker.cancel_all_open_orders(gw)` 撤所有未终态单
     - `trading_service.emergency_halt()` 置 lock_down + 告警
   - 原因：无 equity 数据源的熔断是伪熔断（用 None/0 触发 = 永远不触发 或 误触发）。

2. **策略层数据源注入**（Task 10 `__main__.py` 的 `⚠️ Scope 边界` 段，四触发点内部数据源为 None/空 no-op）：
   - `NecklineMethodStrategy.scan_at(universe)` → `signals`（eod_plan 消费）：当前 `_eod` 传 `signals=[]`（无信号 → 空 orders）。
   - 持仓状态机 `stop_prices` map（stop_loss_monitor 消费）：当前 `_stoploss` 传 `None`（返「无止损价配置」no-op）。
   - `active.json` 真实 `local_positions`（post_close 对账消费）：当前 `_post_close` 传 None（对账跳过，drift 恒为 False 非真对账）。
   - 必修：上线集成阶段接线策略实例化 + universe 拉取 + 持仓状态机读写 + active.json 读取。

3. **若切 EMT 网关（已废弃路径，memory `quanter-emt-broker-access.md`）**：
   - EMT 极速交易网关无 xtdata 行情源，止损链路 `qmt_market_data.get_quote` 返 None → stop_loss_monitor 全跳过（盲止损 = 敞口失控）。
   - 必修：另接行情源（如 EMT 的 `get_quote` API 或第三方 tick 源），或固定使用 miniQMT 通道。

## 切 live 操作（五前置全打勾后）

1. 改 `.env`：`AUTO_TRADE_MODE=live`（其他保持）。
2. 重启 engine 进程：
   - Terminal tab 模式：Ctrl-C 老进程 → 重跑 `scripts/run_trading_engine.bat`
   - PM2：`pm2 restart trading-engine`
   - schtasks：`schtasks /Run /TN "QuanterTradingEngine"`
3. 启动期验证：日志必须出现 WARNING `⚠️ LIVE 模式：将真实下单！确保影子模式已跑满 TRADE_SHADOW_MIN_DAYS(=5) 天...`（`__main__.py` 启动闸）——若未出现说明 `AUTO_TRADE_MODE` 没读到，停进程排查。
4. 首个交易日观察 `pre_open` 日志：`state` 应出现 `FILLED` / `PARTIALLY_FILLED` / `REJECTED`（真实触达网关），不再是 `DRY_RUN`。

> 切 live 后若需回退影子：改 `.env` `AUTO_TRADE_MODE=dry_run` 重启即可（同一进程同一份代码，dry_run/live 仅 env 开关切换，无代码分叉）。

---

## 上线后常驻进程清单（二期新增 1 个）

| 进程 | 职责 | 启动命令见 |
|------|------|-----------|
| `python -m trading`（TradingEngine 常驻） | APScheduler 四 cron 触发点（eod_plan/pre_open/stop_loss/post_close）+ 影子模式 dry_run 分流 | 「四、Step 2」 |
