"""通知管理器：多通道并发 + 单通道软降级 + 级别前缀 + 单例。"""
import asyncio

from core.notifier import NotificationManager, NotificationChannel


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
