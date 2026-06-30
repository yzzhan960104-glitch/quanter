"""通知管理器：多通道并发 + 单通道软降级 + 级别前缀 + 单例。"""
import asyncio

from core.notifier import (
    NotificationManager,
    NotificationChannel,
    TelegramChannel,
    WeComChannel,
    build_default_manager,
)


class _FakeChannel(NotificationChannel):
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.sent = []

    async def send(self, text: str) -> None:
        if self.fail:
            raise RuntimeError(f"{self.name} 发送失败")
        self.sent.append(text)


def test_notify_fans_out_to_all_channels():
    mgr = NotificationManager()
    a, b = _FakeChannel("a"), _FakeChannel("b")
    mgr.add_channel(a)
    mgr.add_channel(b)
    asyncio.run(mgr.notify_risk_event("Tushare 熔断", "ERROR"))
    assert len(a.sent) == 1 and len(b.sent) == 1
    assert "ERROR" in a.sent[0] and "Tushare 熔断" in a.sent[0]


def test_notify_soft_fails_one_channel_without_blocking_others():
    mgr = NotificationManager()
    ok, bad = _FakeChannel("ok"), _FakeChannel("bad", fail=True)
    mgr.add_channel(ok)
    mgr.add_channel(bad)
    # bad 抛异常，但 ok 仍应收到，且不向外抛
    asyncio.run(mgr.notify_risk_event("x", "WARN"))
    assert len(ok.sent) == 1


def test_level_prefix():
    mgr = NotificationManager()
    ch = _FakeChannel("c")
    mgr.add_channel(ch)
    asyncio.run(mgr.notify_risk_event("最大回撤触红线", "CRITICAL"))
    assert "CRITICAL" in ch.sent[0]


def test_singleton():
    a = NotificationManager.get_default()
    b = NotificationManager.get_default()
    assert a is b


def test_build_default_manager_is_idempotent(monkeypatch):
    """build_default_manager 多次调用必须幂等，否则同一通道被重复 append
    会导致一条预警被投递 N 遍（回归守护）。"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT_ID")
    mgr = NotificationManager.get_default()
    mgr.clear_channels()  # 复位单例，避免被其它用例污染
    build_default_manager()
    build_default_manager()  # 二次装配：必须短路、不重复
    build_default_manager()  # 三次：再确认
    # 直接数内部通道数（TelegramChannel 实例），不应重复堆积
    tg = [c for c in mgr._channels if isinstance(c, TelegramChannel)]
    assert len(tg) == 1


def test_telegram_channel_payload(monkeypatch):
    """守护真实 URL 与 JSON payload 拼装（脱网，monkeypatch _http_post）。"""
    captured = {}

    async def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload

    channel = TelegramChannel("BOT_TOKEN", "CHAT_ID")
    # 注入假投递函数，避免真实触网
    channel._http_post = fake_post
    asyncio.run(channel.send("hi"))
    assert captured["url"] == "https://api.telegram.org/botBOT_TOKEN/sendMessage"
    assert captured["payload"] == {
        "chat_id": "CHAT_ID",
        "text": "hi",
        "parse_mode": "Markdown",
    }


def test_wecom_channel_payload(monkeypatch):
    """守护企业微信 webhook URL 与 msgtype/text payload（脱网）。"""
    captured = {}

    async def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload

    channel = WeComChannel("https://qyapi.example.com/webhook")
    channel._http_post = fake_post
    asyncio.run(channel.send("hi"))
    assert captured["url"] == "https://qyapi.example.com/webhook"
    assert captured["payload"] == {"msgtype": "text", "text": {"content": "hi"}}


def test_fire_and_forget_runs_coroutine_in_background():
    """fire_and_forget 必须在无事件循环的同步上下文里也能跑通协程。"""
    from core.notifier import fire_and_forget
    import time as _t
    done = []
    async def _work():
        done.append(42)
    fire_and_forget(_work())
    _t.sleep(0.5)  # 等后台线程
    assert done == [42]


def test_dingtalk_channel_payload_and_sign(monkeypatch):
    """守护钉钉加签 URL 拼装与 Markdown payload（脱网，monkeypatch _post）。"""
    from core.notifier import DingTalkChannel
    captured = {}

    async def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload

    ch = DingTalkChannel("https://oapi.dingtalk.com/robot/send?access_token=XXX", "SEC123")
    ch._post = fake_post
    import asyncio
    asyncio.run(ch.send("最大回撤触红线"))
    # 加签参数必须出现在 URL
    assert "timestamp=" in captured["url"] and "sign=" in captured["url"]
    # Markdown + 固定安全词【Quanter】
    assert captured["payload"]["msgtype"] == "markdown"
    assert "【Quanter】" in captured["payload"]["markdown"]["text"]
    assert "最大回撤触红线" in captured["payload"]["markdown"]["text"]
