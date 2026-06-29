"""SSE 日志管线：跨线程 hub 投递 + handler 入缓冲。"""
import asyncio
import logging

from server.api.v1.logs import LogStreamHub, RingBufferLogHandler, log_stream_hub


def test_hub_publish_reaches_subscriber():
    async def run():
        q = log_stream_hub.subscribe()
        log_stream_hub.publish({"level": "INFO", "message": "hello"})
        rec = await asyncio.wait_for(q.get(), timeout=1.0)
        assert rec["message"] == "hello"
        log_stream_hub.unsubscribe(q)

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
