# 钉钉机器人启动（dws 统一接入）

> 2026-07-16 迁移：bridge 自研（dingtalk-stream + ClaudePool + Alarmer）退役，两个机器人都走 **dws dev connect**，一套接入逻辑。

## 前置
1. **dws 登录**：`dws auth login`（浏览器授权，见 [dingtalk-workspace-cli](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli)）。凭证持久化，30 天有效。
2. **.env 凭证**（已配）：`GLM_API_KEY`（z.ai coding plan）/ `REVIEW_*`（审查 webhook 推报告）/ `DINGTALK_*`（备用）。
3. **Python**：`.venv310/Scripts/python.exe`（Python 3.10）。

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

### 3. uvicorn 服务（training loop + webhook 推 + /review 端点）
```bash
C:/Users/yzzhan/Desktop/quanter/.venv310/Scripts/python.exe -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```
- lifespan 装 TrainingLoopOrchestrator（daemon）+ replay_scheduler
- DingTalkNotifier（webhook 推报告/回显，urllib，不用 dingtalk-stream SDK）

## 职责隔离（多机器人多职责）
每个机器人 = 一个统一应用 + 一个 dws dev connect，职责由 `--channel`/`--agent-cmd` 定：
- `--channel claudecode` = 对话职责（@→Claude Code）
- `--channel custom --agent-cmd <脚本>` = 任意业务职责（@消息作为 argv 喂脚本）
- 加新职责 = 建新统一应用机器人 + 写 agent-cmd 脚本 + 起 dws dev connect

## 退役说明
- `bridge/`（自研 dingtalk-stream + ClaudePool + Alarmer）已删（commit 679d731）
- `dingtalk-stream` SDK 已从 requirements.txt 移除（两机器人都走 dws，无人用）
- `DingTalkNotifier`（webhook 推报告）保留 —— urllib，不走 SDK
