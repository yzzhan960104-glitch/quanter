# -*- coding: utf-8 -*-
"""training_dingtalk 单测：webhook 推报告（@审核改走 dws 桥）。

迁移说明（2026-07-16，dws-migration Task 4）：
  - 原双通道（webhook 推 + dingtalk-stream 收审核）中的 stream 收审核已删，
    @审核消息改走 dws dev connect 桥（dingtalk_review_bridge.py →
    POST /api/v1/training/review → orchestrator.submit_review）。
  - 本文件随之删除 ReviewChatbotHandler / _run_stream / start_review_bot 相关测试，
    仅保留 webhook 推报告链路（ReviewBotConfig / DingTalkNotifier / _NoopNotifier）。
  - ReviewBotConfig.from_env 软降级门控沿用 dws 迁移前语义（app_key/secret/staff 三件套
    缺一 → None），断言保持不变（门控不改，避免连锁影响 lifespan 与既有断言）。

全量 mock：不真发钉钉。urlopen 均替身。
"""
import json
from unittest.mock import MagicMock

from backtest.optimize import training_dingtalk


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
    """缺 REVIEW_APP_KEY → from_env 返 None 软降级（门控条件沿用 dws 迁移前语义）。"""
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
    """webhook 空 → push 仅记日志，不调 urlopen（凭证只配 webhook 推、不推报告时）。"""
    _set_review_env(monkeypatch, webhook="")
    cfg = training_dingtalk.ReviewBotConfig.from_env()
    assert cfg is not None
    assert cfg.webhook == ""

    called = {"n": 0}

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


# -------------------- _NoopNotifier（软降级） --------------------

def test_noop_notifier_does_nothing(monkeypatch):
    """凭证未配 → orchestrator 用 _NoopNotifier，push 静默 no-op，不抛不调网。"""
    called = {"n": 0}

    def boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("Noop 不应触网")

    monkeypatch.setattr(training_dingtalk.urllib.request, "urlopen", boom)
    training_dingtalk._NoopNotifier().push("any", "any text")
    assert called["n"] == 0
