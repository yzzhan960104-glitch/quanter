# -*- coding: utf-8 -*-
"""training_dingtalk 单测：webhook 推报告 + stream 收审核。

方案纠偏（2026-07-15）：brief 原文的 access_token + batch send 已作废，改为
  - 主动推：群自定义机器人 webhook（urllib + 复用 DingTalkChannel._sign 加签）
  - 收审核：企业内部应用 dingtalk-stream（独立 REVIEW_APP_KEY/SECRET）

全量 mock：不真发钉钉、不真起 stream。urlopen / SDK msg / orchestrator 均替身。
"""
import json
from unittest.mock import MagicMock

import pytest

from caisen import training_dingtalk


# -------------------- 配置装配 --------------------

def _set_review_env(monkeypatch, *, webhook="https://oapi.dingtalk.com/robot/send?access_token=TOK",
                    webhook_secret="SECxxx", staff="staffA,staffB",
                    app_key="ak123", app_secret="sk456"):
    """统一注入 REVIEW_* 环境变量（测试用，凭证均为伪造）。"""
    monkeypatch.setenv("REVIEW_APP_KEY", app_key)
    monkeypatch.setenv("REVIEW_APP_SECRET", app_secret)
    monkeypatch.setenv("REVIEW_WEBHOOK", webhook)
    monkeypatch.setenv("REVIEW_WEBHOOK_SECRET", webhook_secret)
    monkeypatch.setenv("REVIEW_ALLOWED_STAFF_IDS", staff)


def test_review_bot_config_from_env_missing_returns_none(monkeypatch):
    """缺 REVIEW_APP_KEY（stream 收审核必需）→ from_env 返 None 软降级。"""
    for k in ("REVIEW_APP_KEY", "REVIEW_APP_SECRET",
              "REVIEW_WEBHOOK", "REVIEW_WEBHOOK_SECRET",
              "REVIEW_ALLOWED_STAFF_IDS"):
        monkeypatch.delenv(k, raising=False)
    assert training_dingtalk.ReviewBotConfig.from_env() is None


def test_review_bot_config_from_env_ok(monkeypatch):
    """凭证齐 → 装配成功，字段映射正确。"""
    _set_review_env(monkeypatch)
    cfg = training_dingtalk.ReviewBotConfig.from_env()
    assert cfg is not None
    assert cfg.app_key == "ak123"
    assert cfg.app_secret == "sk456"
    assert cfg.webhook.startswith("https://oapi.dingtalk.com/robot/send")
    assert cfg.webhook_secret == "SECxxx"
    assert cfg.allowed_staff_ids == ("staffA", "staffB")


# -------------------- DingTalkNotifier（webhook 推） --------------------

def _mock_urlopen(captured: dict, resp_body: dict):
    """构造 urlopen 替身：把 url/body/headers 塞进 captured，回 resp_body。"""
    def fake(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        # req.headers 是 dict（urllib Request 对象属性）；个别版本是 _headers，兼容取
        captured["headers"] = getattr(req, "headers", {}) or {}
        resp = MagicMock()
        resp.read.return_value = json.dumps(resp_body).encode("utf-8")
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=resp)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx
    return fake


def test_notifier_push_posts_to_webhook_with_sign(monkeypatch):
    """webhook_secret 非空 → URL 含 timestamp=&sign=；body 为 markdown 消息；errcode=0 通过。"""
    _set_review_env(monkeypatch)
    cfg = training_dingtalk.ReviewBotConfig.from_env()
    assert cfg is not None

    captured = {}
    monkeypatch.setattr(training_dingtalk.urllib.request, "urlopen",
                        _mock_urlopen(captured, {"errcode": 0, "errmsg": "ok"}))

    n = training_dingtalk.DingTalkNotifier(cfg)
    n.push("loop1", "## 训练报告\nmin_rr=2.0 回测中")

    # 1) POST 打到 webhook url（不是 batch send / oauth）
    assert captured["url"].startswith("https://oapi.dingtalk.com/robot/send")
    # 2) secret 非空 → 加签：url 拼了 timestamp=&sign=
    assert "timestamp=" in captured["url"]
    assert "sign=" in captured["url"]
    # 3) body 是群机器人 markdown 协议
    assert captured["body"]["msgtype"] == "markdown"
    assert "text" in captured["body"]["markdown"]
    assert "title" in captured["body"]["markdown"]
    # 4) 文本经 clean_markdown_for_dingtalk 清洗后仍在 body（标题保留）
    assert "训练报告" in captured["body"]["markdown"]["text"]


def test_notifier_push_without_secret(monkeypatch):
    """webhook_secret 空 → 裸发，url 不含 timestamp/sign。"""
    _set_review_env(monkeypatch, webhook_secret="")
    cfg = training_dingtalk.ReviewBotConfig.from_env()
    assert cfg is not None
    assert cfg.webhook_secret == ""

    captured = {}
    monkeypatch.setattr(training_dingtalk.urllib.request, "urlopen",
                        _mock_urlopen(captured, {"errcode": 0}))

    training_dingtalk.DingTalkNotifier(cfg).push("l", "## r\nx")
    assert "timestamp=" not in captured["url"]
    assert "sign=" not in captured["url"]


def test_notifier_no_webhook_noop(monkeypatch):
    """webhook 空 → push 仅记日志，不调 urlopen（凭证只配 stream 收审核时）。"""
    _set_review_env(monkeypatch, webhook="")
    cfg = training_dingtalk.ReviewBotConfig.from_env()
    assert cfg is not None
    assert cfg.webhook == ""

    called = {"n": 0}
    orig = training_dingtalk.urllib.request.urlopen

    def boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("不应调 urlopen")

    monkeypatch.setattr(training_dingtalk.urllib.request, "urlopen", boom)
    # 不应抛（软降级），也不应调 urlopen
    training_dingtalk.DingTalkNotifier(cfg).push("l", "## r\nx")
    assert called["n"] == 0


def test_notifier_push_errcode_nonzero_does_not_raise(monkeypatch):
    """钉钉返 HTTP 200 + errcode!=0（真实失败模式）：_validate_response 抛但被软降级捕获。"""
    _set_review_env(monkeypatch)
    cfg = training_dingtalk.ReviewBotConfig.from_env()

    captured = {}
    monkeypatch.setattr(training_dingtalk.urllib.request, "urlopen",
                        _mock_urlopen(captured, {"errcode": 310000, "errmsg": "sign not match"}))

    # 不应外抛（push 是附属通道，失败仅日志）
    training_dingtalk.DingTalkNotifier(cfg).push("l", "## r\nx")


# -------------------- ReviewChatbotHandler（stream 收审核） --------------------

def _make_sdk_msg(content="min_rr 改2.0 重跑", sender_staff_id="staffA"):
    """构造一条 SDK ChatbotMessage 替身（字段以 bridge 实测为准）。"""
    msg = MagicMock()
    msg.text.content = content
    msg.sender_staff_id = sender_staff_id
    msg.conversation_id = "c1"
    msg.message_id = "m1"
    return msg


def test_chatbot_handler_whitelist_and_wake(monkeypatch):
    """白名单内 sender → submit_review(loop_id, text)；非白名单 → 静默丢弃。"""
    _set_review_env(monkeypatch)
    cfg = training_dingtalk.ReviewBotConfig.from_env()

    submitted = []
    orch = MagicMock()
    orch.active_loop_id = "loop1"
    orch.submit_review = lambda lid, text: submitted.append((lid, text))

    h = training_dingtalk.ReviewChatbotHandler(cfg, orch)

    # 白名单内 → 唤醒
    h._dispatch(_make_sdk_msg("min_rr 改2.0 重跑", "staffA"))
    assert submitted == [("loop1", "min_rr 改2.0 重跑")]

    # 非白名单 → 不唤醒
    submitted.clear()
    h._dispatch(_make_sdk_msg("haha", "intruder"))
    assert submitted == []


def test_chatbot_handler_strips_at_prefix(monkeypatch):
    """@机器人 前缀被剥离后再喂给 orchestrator（同 BridgeHandler）。"""
    _set_review_env(monkeypatch)
    cfg = training_dingtalk.ReviewBotConfig.from_env()
    submitted = []
    orch = MagicMock()
    orch.active_loop_id = "loop1"
    orch.submit_review = lambda lid, text: submitted.append((lid, text))
    h = training_dingtalk.ReviewChatbotHandler(cfg, orch)

    h._dispatch(_make_sdk_msg("@审查bot min_rr=1.8 重跑", "staffA"))
    assert submitted == [("loop1", "min_rr=1.8 重跑")]


def test_chatbot_handler_no_active_loop_no_submit(monkeypatch):
    """无活跃 loop（active_loop_id=None）→ 收到审核也不 submit（防误触）。"""
    _set_review_env(monkeypatch)
    cfg = training_dingtalk.ReviewBotConfig.from_env()
    submitted = []
    orch = MagicMock()
    orch.active_loop_id = None
    orch.submit_review = lambda lid, text: submitted.append((lid, text))
    h = training_dingtalk.ReviewChatbotHandler(cfg, orch)

    h._dispatch(_make_sdk_msg("min_rr 改2.0", "staffA"))
    assert submitted == []


def test_chatbot_handler_process_acks_immediately(monkeypatch):
    """process 立即 ACK（返 STATUS_OK），重活丢后台 task（不阻塞 SDK 主循环）。"""
    _set_review_env(monkeypatch)
    cfg = training_dingtalk.ReviewBotConfig.from_env()
    orch = MagicMock()
    orch.active_loop_id = "loop1"
    h = training_dingtalk.ReviewChatbotHandler(cfg, orch)

    # monkeypatch ChatbotMessage.from_dict 返我们的 mock msg
    from dingtalk_stream import AckMessage, ChatbotMessage
    fake_msg = _make_sdk_msg("hi", "staffA")
    monkeypatch.setattr(ChatbotMessage, "from_dict", lambda d: fake_msg)

    callback = MagicMock()
    callback.data = {"raw": "x"}

    import asyncio
    loop = asyncio.new_event_loop()
    try:
        code, message = loop.run_until_complete(h.process(callback))
    finally:
        loop.close()

    from dingtalk_stream import AckMessage as _Ack
    assert code == _Ack.STATUS_OK
    # submit_review 由后台 task 异步触发；等一拍让 task 跑完再断言
    # （process 已 create_task，事件循环关闭前未必跑完；此处只验 ACK 语义，
    #  submit 的真实唤醒由 _dispatch 的同步测试覆盖）


# -------------------- _NoopNotifier（软降级） --------------------

def test_noop_notifier_does_nothing(monkeypatch):
    """凭证未配 → orchestrator 用 _NoopNotifier，push 静默 no-op，不抛不调网。"""
    called = {"n": 0}
    orig = training_dingtalk.urllib.request.urlopen

    def boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("Noop 不应触网")

    monkeypatch.setattr(training_dingtalk.urllib.request, "urlopen", boom)
    training_dingtalk._NoopNotifier().push("any", "any text")
    assert called["n"] == 0


# -------------------- start_review_bot（lifespan 装配） --------------------

def test_start_review_bot_soft_degrade_when_no_creds(monkeypatch):
    """凭证未配 → start_review_bot 在 from_env 即短路返 None（不触 create_task，无需事件循环）。"""
    for k in ("REVIEW_APP_KEY", "REVIEW_APP_SECRET",
              "REVIEW_WEBHOOK", "REVIEW_WEBHOOK_SECRET",
              "REVIEW_ALLOWED_STAFF_IDS"):
        monkeypatch.delenv(k, raising=False)
    task = training_dingtalk.start_review_bot(app=MagicMock(), orchestrator=MagicMock())
    assert task is None


def test_start_review_bot_starts_task_when_creds_ok(monkeypatch):
    """凭证齐 + 运行中的事件循环 → 起 stream 后台 task（mock _run_stream 防真连钉钉）。"""
    _set_review_env(monkeypatch)
    started = {}

    async def _fake_run(cfg, orch):
        started["cfg"] = cfg
        started["orch"] = orch

    monkeypatch.setattr(training_dingtalk, "_run_stream", _fake_run)

    # start_review_bot 内部 asyncio.create_task 需要 running event loop；
    # 包一层 async helper，用 run_until_complete 驱动，保证 loop 处于 running 状态。
    import asyncio

    async def _driver():
        task = training_dingtalk.start_review_bot(
            app=MagicMock(), orchestrator="ORCH"
        )
        assert task is not None
        await task  # 等 _fake_run 跑完

    asyncio.new_event_loop().run_until_complete(_driver())
    assert started["cfg"].app_key == "ak123"
    assert started["orch"] == "ORCH"
