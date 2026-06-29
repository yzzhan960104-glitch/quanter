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
