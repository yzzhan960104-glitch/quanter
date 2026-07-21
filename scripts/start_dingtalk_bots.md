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
