"""SSE 日志管线：跨线程 hub 投递 + handler 入缓冲。"""
import asyncio
import logging

from server.api.v1.logs import LogStreamHub, RingBufferLogHandler, log_stream_hub


def test_hub_publish_reaches_subscriber():
    # 用独立 hub 而非模块级单例 log_stream_hub：单例会被 test_backtest_stream 触发的
    # 回测日志（经 RingBufferLogHandler）污染缓冲，subscribe 回放历史会令 q.get() 取到
    # 回测日志而非本测试 publish 的 "hello"。独立 hub 隔离测试，守住 publish→订阅者投递契约。
    hub = LogStreamHub()

    async def run():
        q = hub.subscribe()
        hub.publish({"level": "INFO", "message": "hello"})
        rec = await asyncio.wait_for(q.get(), timeout=1.0)
        assert rec["message"] == "hello"
        hub.unsubscribe(q)

    asyncio.run(run())


def test_ring_buffer_handler_feeds_hub():
    hub = LogStreamHub()
    handler = RingBufferLogHandler(hub, level=logging.INFO)
    logger = logging.getLogger("test.ring")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("回测启动")
    # handler.emit 同步把记录写入 hub 缓冲
    assert any("回测启动" in r["message"] for r in list(hub._buffer))


def test_hub_buffer_replays_to_new_subscriber():
    hub = LogStreamHub()
    hub.publish({"level": "INFO", "message": "历史"})
    async def run():
        q = hub.subscribe()
        rec = await asyncio.wait_for(q.get(), timeout=1.0)
        assert rec["message"] == "历史"
    asyncio.run(run())


def test_publish_removes_dead_subscriber_when_loop_closed():
    """死订阅者清理：loop 关闭后 publish 应把该 (q, loop) 从 _subs 移除。

    复现 SSE 客户端断开的真实场景：subscribe 在一个事件循环里完成，随后该循环
    关闭；下一轮 publish 对其 call_soon_threadsafe 会抛 RuntimeError，必须在
    except 分支内把它清理掉，否则 _subs 无界增长。
    """
    hub = LogStreamHub()

    # 在一个独立事件循环里完成订阅，拿到队列与对应的死 loop 引用
    def subscribe_then_close():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            q = loop.run_until_complete(_do_subscribe(hub))
        finally:
            loop.close()  # 关闭循环 → 该订阅者变“死”
        return q

    async def _do_subscribe(h):
        return h.subscribe()

    q1 = subscribe_then_close()
    # 订阅成功，应有 1 个订阅者
    assert len(hub._subs) == 1

    # publish 时该死订阅者会触发 RuntimeError → 应被即时清理
    hub.publish({"level": "INFO", "message": "trigger-cleanup"})

    # 死订阅者已从 _subs 移除，且其队列不在集合内
    assert len(hub._subs) == 0
    assert all(sq is not q1 for (sq, sl) in hub._subs)


def test_sse_event_gen_emits_ping_on_idle(monkeypatch):
    """#12：无日志时 _sse_event_gen 在超时后 yield ': ping' 保活（防代理断开空闲连接）。

    物理意图：原 q.get() 无超时永久阻塞，SSE 连接无数据流动会被 nginx/代理 60s 静默断开。
    wait_for 超时发 SSE 注释帧 ': ping\\n\\n'（客户端不触发 message，仅保连接活跃）。
    用独立空 hub（无历史缓冲）+ 极小超时加速测试。
    """
    import server.api.v1.logs as logs_mod
    from server.api.v1.logs import _sse_event_gen, LogStreamHub

    monkeypatch.setattr(logs_mod, "_SSE_KEEPALIVE_TIMEOUT", 0.05)
    empty_hub = LogStreamHub()   # 空缓冲：subscribe 后 q.get() 无历史可取 → 必走 ping 路径
    monkeypatch.setattr(logs_mod, "log_stream_hub", empty_hub)

    async def run():
        gen = _sse_event_gen()
        return await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    frame = asyncio.run(run())
    assert frame == ": ping\n\n"
