# Task 4 报告：删审查机器人老 stream 死代码（@接收改 dws 桥）

**分支**：`bridge-dws-migration`
**日期**：2026-07-16
**状态**：DONE

> 注：本报告覆盖之前的「Task 4 创建审查机器人」报告（同名文件）。那次创建了 webhook 推 +
> stream 收审核双通道；本次 dws-migration Task 4 是删除其中的 stream 收审核死代码。

## 背景

审查机器人（yzzhan 参数优化，统一应用 dingbabujxcelmssmdpn）的 @接收已于 2026-07-16
改走 dws dev connect 桥（`scripts/dingtalk_review_bridge.py` → POST `/api/v1/training/review`
→ `orchestrator.submit_review`）。因此 `caisen/optimize/training_dingtalk.py` 里老的
`dingtalk-stream` SDK 收审核代码（ReviewChatbotHandler / start_review_bot / _run_stream /
import dingtalk_stream）变为死代码，本任务负责删除。

## 做了什么

### 1. `caisen/optimize/training_dingtalk.py`
- **顶部 docstring 重写**：原「webhook + stream 双通道」改为「@审核改走 dws 桥，本模块仅保留
  webhook 推报告」。说明清楚保留了 ReviewBotConfig / DingTalkNotifier / _NoopNotifier 三个实体，
  以及 `ReviewBotConfig.from_env` 的「app_key/secret/staff 三件套缺一 → None」软降级门控
  **故意不改**（避免连锁影响 lifespan 与既有测试断言）。
- **删除死代码**（原 line 173-末尾）：
  - `import dingtalk_stream` / `from dingtalk_stream import AckMessage, ChatbotMessage`
  - `class ReviewChatbotHandler(dingtalk_stream.ChatbotHandler)`
  - `async def _run_stream`
  - `def start_review_bot`
- **清理未用 import**：`asyncio`（仅 _run_stream/start_review_bot 用过）、`typing.Any`
  （仅 _dispatch/start_review_bot 签名用过）一并删除。保留 `json`/`urllib.request`/`urllib.error`
  /`Optional`（DingTalkNotifier + from_env 仍用）。
- **ReviewBotConfig docstring 轻量更新**：`app_key/app_secret` 标注为「历史遗留字段，实际推送
  链路不读它们」，避免与删除 stream 后的事实矛盾。
- 文件从 288 行 → 170 行。

### 2. `server/main.py`（lifespan）
- **import 段**：`from caisen.training_dingtalk import (...)` 去掉 `start_review_bot`
  （保留 ReviewBotConfig / DingTalkNotifier / _NoopNotifier）。
- **startup 装配块**：删 `if _review_cfg is not None: app.state.review_bot_task =
  start_review_bot(...)` 整个分支 + 对应的 else info 日志；保留
  `app.state.training_orchestrator = TrainingLoopOrchestrator(_notifier)` +
  `start_daemon()`。新增注释说明 @审核改走 dws 桥、此处只装 webhook notifier + daemon。
- **shutdown 段**：删 `_rbtask = getattr(..., "review_bot_task", None); if _rbtask: _rbtask.cancel()`，
  保留 `_orch.stop_daemon()`。注释同步更新（不再有 review_bot_task 需要 cancel）。
- **startup 注释块顶部**：把「orchestrator daemon 线程 + review bot stream 均寄生主进程」
  改为「orchestrator daemon 线程寄生主进程；dws-migration Task 4 后不再起 stream 审核机器人」。

### 3. `tests/caisen/test_training_dingtalk.py`
- 删除 ReviewChatbotHandler / start_review_bot / _run_stream 相关测试：
  - `_make_sdk_msg` helper
  - `test_chatbot_handler_whitelist_and_wake`
  - `test_chatbot_handler_strips_at_prefix`
  - `test_chatbot_handler_no_active_loop_no_submit`
  - `test_chatbot_handler_process_acks_immediately`
  - `test_start_review_bot_soft_degrade_when_no_creds`
  - `test_start_review_bot_starts_task_when_creds_ok`
- 保留：`test_review_bot_config_from_env_*`（2）、`test_notifier_*`（4）、
  `test_noop_notifier_does_nothing`（1）= 共 7 个测试（含 _mock_urlopen/_set_review_env helper）。
- 顶部 docstring 更新为「@审核改走 dws 桥」迁移说明。

### 4. `caisen/optimize/training_loop.py`（顺手修正）
- `active_loop_id` 属性 docstring 原写「供 ReviewChatbotHandler 把 @消息路由到正确 loop」，
  改为「供 dws 桥把 @审核消息路由到正确 loop」（含 dingtalk_review_bridge.py →
  POST /api/v1/training/review 路径说明）。纯注释修正，无逻辑改动。

## 测试命令 + 结果

```bash
PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -m pytest \
  tests/caisen/test_training_dingtalk.py \
  tests/caisen/test_training_loop.py \
  tests/test_training_api.py -q
```

**结果**：`19 passed, 1 warning in 3.32s`（warning 为 fastapi/httpx 已知 deprecation，与本次改动无关）

## Smoke 验证

```bash
PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -c "from server.main import app; ..."
```

- `from server.main import app` **无异常**（review_bot_stream 不再装配，DingTalkNotifier 仍在）。
- OpenAPI 38 路径，training 5 端点全部就位：`/api/v1/training`、`/review`、`/start`、`/{loop_id}`、
  `/{loop_id}/stop`。
- 断言死代码确实删除：`caisen.optimize.training_dingtalk` 不再有 `start_review_bot`/
  `ReviewChatbotHandler`/`_run_stream`；`DingTalkNotifier`/`_NoopNotifier`/`ReviewBotConfig` 保留。

## Commit

见 `git log`（commit message：`refactor(dws-migration): Task4 删审查老stream死代码(@接收改dws桥)`）。

## Concerns（次要，非阻断）

1. **`scripts/verify_dingtalk_review.py`**（未跟踪新文件，git status `??`，不在本次 Task 4 范围）：
   - 它是 @用户自建的「审查 stream 通道」独立验证脚本，仍 `import dingtalk_stream` + 用
     `ChatbotHandler`，且 line 55 注释提到 "spec3 ReviewChatbotHandler 可据此唤醒 loop"。
   - dws 迁移后这脚本验证的是**已废弃的老通道**（应改去验证 dws 桥）。
   - 本次未改动它（不在删改清单），建议后续单独决定：删除 / 改造为验证 dws 桥 / 保留作为
     app 凭证连通性 smoke。

2. **`bridge/stream_client.py`** 仍 `import dingtalk_stream`：这是 **@claude 桥**的 stream
   （完全独立应用，非审查机器人），保留正确，无任何问题。

3. **`ReviewBotConfig.from_env` 门控未改**：仍要求 `app_key/secret/staff` 三件套齐全才装配。
   dws 迁移后 webhook 推报告其实只需要 `webhook`/`webhook_secret`，`app_*` 已是历史遗留。
   本任务**故意不改门控**（避免连锁影响 server/main.py lifespan 与既有 7 个测试断言），
   顶部 docstring 已显式说明此决策。如未来想让 webhook-only 配置也能装配 notifier，需单独评估。

## 相关文件（绝对路径）
- 实现（删改）：`C:\Users\yzzhan\Desktop\quanter\caisen\optimize\training_dingtalk.py`
- lifespan（删装配）：`C:\Users\yzzhan\Desktop\quanter\server\main.py`
- 测试（删测试）：`C:\Users\yzzhan\Desktop\quanter\tests\caisen\test_training_dingtalk.py`
- 注释修正：`C:\Users\yzzhan\Desktop\quanter\caisen\optimize\training_loop.py`
- 垫片（未动）：`C:\Users\yzzhan\Desktop\quanter\caisen\training_dingtalk.py`
- dws 桥（新通道，不在本任务）：`C:\Users\yzzhan\Desktop\quanter\scripts\dingtalk_review_bridge.py`
