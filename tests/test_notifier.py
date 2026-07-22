"""通知管理器：多通道并发 + 单通道软降级 + 级别前缀 + 单例。"""
import asyncio

import pytest

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


def test_fire_and_forget_inside_running_loop_executes_coro():
    """loop 内（async 上下文）调 fire_and_forget 也应执行协程。

    回归网关 _reconnect 场景：async 函数内调 fire_and_forget，验证新 daemon
    线程 asyncio.run 在 loop 上下文仍正常跑（不抛 RuntimeError 吞协程）。
    若此测试 FAIL，说明 fire_and_forget 需改自适应（loop 内 create_task）。
    """
    from core.notifier import fire_and_forget
    done = []

    async def _work():
        done.append(99)

    async def _runner():
        fire_and_forget(_work())   # loop 内（async 上下文）
        await asyncio.sleep(0.5)   # 等 daemon 线程跑完

    asyncio.run(_runner())
    assert done == [99], "loop 内 fire_and_forget 应执行协程"


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


def test_build_default_manager_includes_dingtalk(monkeypatch):
    """配齐钉钉凭证后 build_default_manager 必须装配 DingTalkChannel（幂等）。"""
    from core.notifier import build_default_manager, DingTalkChannel
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=X")
    monkeypatch.setenv("DINGTALK_SECRET", "SEC")
    mgr = NotificationManager.get_default()
    mgr.clear_channels()
    build_default_manager()
    dts = [c for c in mgr._channels if isinstance(c, DingTalkChannel)]
    assert len(dts) == 1
    build_default_manager()  # 幂等
    dts2 = [c for c in mgr._channels if isinstance(c, DingTalkChannel)]
    assert len(dts2) == 1


def test_build_default_manager_skips_dingtalk_without_credentials(monkeypatch):
    """缺凭证必须跳过该通道，不报错。"""
    from core.notifier import build_default_manager, DingTalkChannel
    monkeypatch.delenv("DINGTALK_WEBHOOK", raising=False)
    monkeypatch.delenv("DINGTALK_SECRET", raising=False)
    mgr = NotificationManager.get_default()
    mgr.clear_channels()
    build_default_manager()
    assert not any(isinstance(c, DingTalkChannel) for c in mgr._channels)


# ============ I-1 审查跟进：钉钉 errcode 业务态校验 ============
# Why 单独覆盖：钉钉 webhook 真实失败模式是 HTTP 200 + body errcode!=0（加签错/
# IP 白名单/关键词/频控）。若只判 raise_for_status 会把所有业务失败都判为"投递
# 成功"——熔断/最大回撤/敞口告警静默丢失，风控最后一道防线形同虚设。


def test_dingtalk_validate_response_rejects_nonzero_errcode():
    """errcode!=0 必须抛 RuntimeError（含 errcode 与 errmsg 上下文）。"""
    from core.notifier import DingTalkChannel
    # 加签失败是最高频的红线相邻故障
    with pytest.raises(RuntimeError) as exc_info:
        DingTalkChannel._validate_response({"errcode": 310000, "errmsg": "sign not match"})
    msg = str(exc_info.value)
    assert "310000" in msg and "sign not match" in msg
    # IP 白名单（常见于生产部署换出口 IP 后告警全部静默）
    with pytest.raises(RuntimeError):
        DingTalkChannel._validate_response({"errcode": 310002, "errmsg": "ip not in white list"})
    # 频控（连环告警触发钉钉限流）
    with pytest.raises(RuntimeError):
        DingTalkChannel._validate_response({"errcode": 130101, "errmsg": "rate limited"})


def test_dingtalk_validate_response_accepts_success_and_tolerant():
    """errcode==0 视为成功不抛；缺 errcode 字段保守放行（避免误杀）。"""
    from core.notifier import DingTalkChannel
    # 成功
    DingTalkChannel._validate_response({"errcode": 0, "errmsg": "ok"})
    # 缺 errcode 字段（极少数 SDK 不回包）——保守放行，避免误杀
    DingTalkChannel._validate_response({})
    # 注意：errcode 字符串 "0" 不等于 int 0，会被判失败——这是钉钉文档约定的
    # 整数 errcode，正常不会出现字符串；此处用例不覆盖该边界。


def test_dingtalk_post_raises_on_business_errcode(monkeypatch):
    """端到端：mock aiohttp 返回 HTTP 200 + errcode!=0，_post 必须抛 RuntimeError。

    守护红线：钉钉业务失败（HTTP 200）必须被识别为投递失败，不能静默成功。
    构造一个假的 ClientSession.post async context manager，其 __aexit__ 返回的
    response 对象 raise_for_status 是 no-op、json() 返回 errcode!=0 的 dict。
    """
    from core.notifier import DingTalkChannel
    import aiohttp

    class _FakeResp:
        def raise_for_status(self):
            # aiohttp.ClientResponse.raise_for_status 是同步方法；模拟 HTTP 200 不抛
            # （钉钉业务失败的典型特征：HTTP 200 + body errcode!=0）
            pass

        async def json(self, content_type=None):
            # 返回加签失败的业务 body（HTTP 200 下的真实失败负载）
            return {"errcode": 310000, "errmsg": "sign not match"}

    class _FakePostCtx:
        # session.post(...) 返回的对象需同时支持 async with 与 __aexit__
        async def __aenter__(self):
            return _FakeResp()

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, **kwargs):
            return _FakePostCtx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    # 注入假的 aiohttp.ClientSession，避免真实触网
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)

    ch = DingTalkChannel("https://oapi.dingtalk.com/robot/send?access_token=X", "SEC")
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(ch._post("https://x", {"msgtype": "markdown"}))
    assert "310000" in str(exc_info.value)


# ============ 钉钉卡片结构化渲染（美观优化） ============


def test_dingtalk_markdown_is_structured():
    """钉钉卡片须结构化：H1 品牌 + 引用块级别徽标 + 正文 + 品牌脚注。"""
    from core.notifier import DingTalkChannel
    md = DingTalkChannel._render_markdown("🚨 [CRITICAL] 最大回撤触红线 12.3%")
    assert md["title"] == "【Quanter】风控告警"
    text = md["text"]
    assert text.startswith("### 【Quanter】风控告警")   # H1 品牌标题
    assert "> 🚨 [CRITICAL]" in text                    # 级别徽标渲染为引用块
    assert "最大回撤触红线 12.3%" in text               # 正文保留
    assert text.endswith("> Quanter · 量化风控网关")    # 品牌脚注


def test_dingtalk_render_plain_text_has_no_level_badge():
    """无级别前缀的裸文本（直调 send）：不渲染级别徽标，仅品牌标题+正文+脚注。"""
    from core.notifier import DingTalkChannel
    md = DingTalkChannel._render_markdown("裸文本消息")
    text = md["text"]
    assert "裸文本消息" in text
    # 仅品牌脚注一处引用块，无级别徽标行
    assert text.count("> ") == 1
