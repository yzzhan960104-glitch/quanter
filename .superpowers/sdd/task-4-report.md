# Task 4 报告：training_dingtalk 参数审查机器人（Spec 3 · 4/5）

## 状态
**DONE** — 13/13 单测绿，全 caisen 套件 197 绿零回归。webhook 方案纠偏已落地。

## 提交
- `feat(training): Task4 钉钉审查机器人 webhook推报告+stream收审核`

## 方案纠偏（权威，覆盖 brief）
brief 原文写的 **access_token 换取 + batch send 推单聊** —— **作废**。
实际实现按用户 2026-07-15 提供的独立凭证，改为 **webhook 推报告 + stream 收审核** 双通道：

| 通道 | 凭证 | 用途 | 复用设施 |
|---|---|---|---|
| webhook 推 | `REVIEW_WEBHOOK` + `REVIEW_WEBHOOK_SECRET`（群自定义机器人） | 主动推 Markdown 报告/回显（单向） | `DingTalkChannel._sign` 加签 + `_validate_response` errcode 校验 + `clean_markdown_for_dingtalk` 清洗 |
| stream 收 | `REVIEW_APP_KEY` + `REVIEW_APP_SECRET`（企业内部应用） | 收 @机器人的审核指令（双向） | 仿 `BridgeHandler` 的 ACK 范式 + 白名单 |

**Why webhook 而非 batch send**：群自定义机器人是单向推（无需 access_token 缓存，配置仅 webhook+secret 两值），完全满足「训练后推报告给研究员审核」场景；batch send 需 access_token 换取+缓存+单聊 userId 列表，复杂度与收益不匹配。stream 仍用企业内部应用（双向收消息必须）。

## 实现（caisen/training_dingtalk.py，5 组件）

1. **ReviewBotConfig**（frozen dataclass + `from_env`）：5 字段（app_key/app_secret/webhook/webhook_secret/allowed_staff_ids）。软降级门控：stream 三件套（app_key/secret/staff）缺一返 None；webhook/webhook_secret 缺失不阻断装配（仅 push 降级 no-op）。

2. **DingTalkNotifier**（webhook 推，实现 TrainingNotifier Protocol）：
   - `push(loop_id, text)`：webhook 空 → no-op；否则 clean → title（首行去 # 前 40 字）→ secret 非空则 `_sign` 加签拼 url → urllib POST → `_validate_response` 校验 errcode。
   - 全程 `except Exception` 软降级（推送是附属通道，失败仅 warning 不反拖垮 loop）。
   - **极简**：纯 urllib，不引 requests/aiohttp 黑盒。

3. **_NoopNotifier**：凭证未配时 orchestrator 用的哑通知器，push 静默 no-op。

4. **ReviewChatbotHandler**（继承 `dingtalk_stream.ChatbotHandler`）：
   - `process`：立即 ACK（STATUS_OK）+ `asyncio.create_task(_safe_dispatch)`（仿 BridgeHandler，防钉钉重投）。
   - `_dispatch`：取 text.content → 去 @前缀 → sender_staff_id 白名单校验 → `orchestrator.active_loop_id` 非空才 `submit_review(loop_id, text)`（防误触）。

5. **start_review_bot(app, orchestrator)**：lifespan 装配入口。`from_env()` 返 None → 返 None 软降级；否则 `asyncio.create_task(_run_stream(cfg, orchestrator))` 返 task。`_run_stream` 用独立 Credential + DingTalkStreamClient + register_callback_handler(ChatbotMessage.TOPIC, handler) + `await client.start()`（不用 start_forever，bridge 踩过的坑）。

## 测试（tests/caisen/test_training_dingtalk.py，13 测试）
- `test_review_bot_config_from_env_missing_returns_none` / `_ok`：from_env 软降级 + 字段映射。
- `test_notifier_push_posts_to_webhook_with_sign`：secret 非空 → url 含 timestamp=&sign= + body msgtype=markdown + text 含清洗后内容。
- `test_notifier_push_without_secret`：secret 空 → 裸发，url 不含 timestamp/sign。
- `test_notifier_no_webhook_noop`：webhook 空 → push 不调 urlopen。
- `test_notifier_push_errcode_nonzero_does_not_raise`：HTTP 200 + errcode!=0 → 软降级不外抛。
- `test_chatbot_handler_whitelist_and_wake`：白名单内 → submit_review；非白名单 → 丢弃。
- `test_chatbot_handler_strips_at_prefix`：@机器人 前缀剥离。
- `test_chatbot_handler_no_active_loop_no_submit`：active_loop_id=None → 不 submit。
- `test_chatbot_handler_process_acks_immediately`：process 返 STATUS_OK。
- `test_noop_notifier_does_nothing`：_NoopNotifier 不触网。
- `test_start_review_bot_soft_degrade_when_no_creds` / `_starts_task_when_creds_ok`：lifespan 装配软降级 + task 起动（mock _run_stream 防真连钉钉）。

全量 mock：urlopen / SDK msg / orchestrator / _run_stream 均替身，不真发钉钉、不真起 stream。

## TDD 证据
1. **RED**：先写 13 测试 → `ImportError: No module named 'caisen.training_dingtalk'`（模块不存在，全部 fail）。
2. **GREEN**：实现 5 组件 → 13/13 PASSED in 0.52s。
3. **回归**：`python -m pytest tests/caisen/ -q` → **197 passed** 零回归。

## 自审（红线核查）
- [x] **webhook 加签复用对**：`DingTalkChannel._sign(secret)→(ts,sign)` + `url&timestamp={ts}&sign={sign}`，与 notifier.py:DingTalkChannel.send 拼法完全一致（HMAC-SHA256+base64+quote_plus 钉钉官方算法）。
- [x] **errcode 校验**：复用 `_validate_response`，HTTP 200+errcode!=0 抛 RuntimeError，被 push except 软降级捕获（测试覆盖）。
- [x] **白名单**：sender_staff_id 优先退化 sender_id，非白名单静默丢弃（防他人触发训练消耗算力）。
- [x] **stream ACK 范式**：process 立即 STATUS_OK + create_task 异步派发（仿 BridgeHandler，防钉钉重投）。
- [x] **_NoopNotifier 软降级**：凭证未配 push 静默 no-op，不触网不抛。
- [x] **from_env 软降级**：stream 三件套缺一返 None，不阻断 uvicorn。
- [x] **极简**：纯 urllib，复用现成加签/校验/清洗，零新依赖。
- [x] **全中文注释**：What+Why（物理意图/算法推导/边界考量）。

## 边界考量
- webhook url 拼接 `&timestamp=` 假定原 webhook 已含 `?access_token=` query（钉钉群机器人标准格式）。若误配无 query 的 url 会首字符错，属配置错误范畴，符合极简原则不做防御。
- stream 收审核与 bridge 物理隔离（独立 REVIEW_APP_KEY/SECRET），不共享连接、不互扰。

## 相关文件（绝对路径）
- 实现：`C:\Users\yzzhan\Desktop\quanter\caisen\training_dingtalk.py`
- 测试：`C:\Users\yzzhan\Desktop\quanter\tests\caisen\test_training_dingtalk.py`
- 复用：`C:\Users\yzzhan\Desktop\quanter\core\notifier.py`（DingTalkChannel._sign/_validate_response）
- 复用：`C:\Users\yzzhan\Desktop\quanter\bridge\replier.py`（clean_markdown_for_dingtalk）
- 范式参考：`C:\Users\yzzhan\Desktop\quanter\bridge\stream_client.py`（BridgeHandler ACK 范式）

## 后续（Task 5 接线）
- `main.py` lifespan 调 `start_review_bot(app, orchestrator)` 装配，task 挂 `app.state.review_bot_task`。
- orchestrator 需补 `@property active_loop_id`（从 `list_active_loops()[0]` 取当前活跃 loop）。
- orchestrator 的 notifier 装配：凭证齐 → `DingTalkNotifier(cfg)`；不齐 → `_NoopNotifier()`。
- 端到端：训练完成 → notifier.push(loop_id, report_md) → 研究员群内看到报告 → @审查bot 发审核指令 → stream 收到 → submit_review 唤醒 loop。
