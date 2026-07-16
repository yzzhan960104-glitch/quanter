# 每日行情播报机器人 · 部署 setup（dws 全自动建号 + 拉群）

> 一次性部署。机器人已建则跳过对应步骤。**前置：`dws auth login`（浏览器授权，仅一次）**。
> 实测于 2026-07-16（分支 `feat/daily-market-brief` Task 0）。

## 凭证（实测拿到，已写 `.env`）

| 用途 | 值 | .env 键 |
|---|---|---|
| 机器人 robotCode | `dingdya5o94mnmde7jlv` | `DINGTALK_CHAT_ROBOT_CODE` |
| yzzhan量化群 openConversationId | `ciduznBwLLiWKcMewBOF4+kWQ==` | `BROADCAST_GROUP_ID` |

## 步骤

### 1. 建机器人（异步，拿 robotCode）

```bash
dws dev app robot submit \
  --name "每日行情播报" \
  --robot-name "行情播报" \
  --desc "每日19点推送A股行情播报摘要" \
  -y
# 返回 taskId → 轮询 result 到 SUCCESS 拿 robotCode
dws dev app robot result --task-id <taskId> -y
```

> ⚠️ **desc 踩坑（errorCode 67010）**：描述只能含中文 / 英文字母 / 数字 / 指定标点，**不能有 `/` `:`** 等。失败时带 `--task-id <原taskId>` 复用重试（不生成新任务）。

### 2. 拉进 yzzhan量化群

```bash
dws chat group members add-bot \
  --robot-code dingdya5o94mnmde7jlv \
  --id "ciduznBwLLiWKcMewBOF4+kWQ==" -y
```

### 3. 验出站（真发一条测试）

```bash
dws chat message send-by-bot \
  --robot-code dingdya5o94mnmde7jlv \
  --group "ciduznBwLLiWKcMewBOF4+kWQ==" \
  --title "播报通道验证" --text "测试" -y
# success=true 即通道 OK
```

### 4. 写 `.env`

```dotenv
DINGTALK_CHAT_ROBOT_CODE=dingdya5o94mnmde7jlv
BROADCAST_GROUP_ID=ciduznBwLLiWKcMewBOF4+kWQ==
```

## 幂等（重复部署时跳过）

- 机器人已建：`dws dev app list` 查到「每日行情播报」→ 跳过 Step 1。
- 已在群：`dws chat group bots --id "ciduznBwLLiWKcMewBOF4+kWQ=="` 查到该 robotCode → 跳过 Step 2。

## 备注

- 机器人 `publicUseReady=false`（未上架）**不影响 `send-by-bot` 出站**——OAuth 凭证 + robotCode 发群消息不依赖发布。
- 「发布版本」（`dev app version` + unifiedAppId）仅在需要"用户主动搜索添加机器人"时才做，播报场景不需要，故 Task 0 不做。
- `clientSecret`（robot result 返回）非必需——`send-by-bot` 走 dws auth OAuth，不用 secret；仅 `dev connect` 本地 stream 建联时才用。
