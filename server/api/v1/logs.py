"""
server/api/v1/logs.py
=====================
实时日志 SSE 端点（Server-Sent Events，单向 server→client）。

为什么用 SSE 而非 WebSocket：日志是单向推送场景，SSE 更轻（HTTP 长连接 +
text/event-stream），契合 Karpathy 极简；WebSocket 的双向/帧协议对本需求属过度设计。

跨线程关键点：回测业务跑在线程池（run_in_threadpool）里，logging.emit 发生在
工作线程；而 SSE 消费在事件循环线程。asyncio.Queue 不是线程安全的，故 publish
必须用 loop.call_soon_threadsafe 把 put_nowait 投递到订阅者所在的事件循环，
否则会破坏事件循环的内部状态（竞态导致队列损坏或回调丢失）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from server.api.v1._sse import sse_dumps

router = APIRouter(prefix="/logs", tags=["实时日志"])

logger = logging.getLogger(__name__)


class LogStreamHub:
    """日志扇出中枢：环形缓冲 + 多订阅者队列，跨线程安全投递。"""

    def __init__(self, maxlen: int = 1000) -> None:
        # 环形缓冲：上限条数自动淘汰最旧记录，内存占用有界，供新订阅者回放历史
        self._buffer: deque[dict] = deque(maxlen=maxlen)
        # 订阅者 = (队列, 其所在事件循环)，便于跨线程 call_soon_threadsafe 精准投递
        self._subs: set[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = set()
        # threading.Lock 而非 asyncio.Lock：publish 由工作线程同步调用，必须线程级互斥
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        """在事件循环线程内调用：注册一个队列，并回放历史缓冲。

        Why 捕获 get_running_loop：订阅发生在 SSE 端点的 async 上下文里，
        此处拿到的 loop 即消费端事件循环；后续 publish 跨线程时用它做 call_soon_threadsafe。
        """
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            # 先回放历史到队列，再加入订阅集——保证不丢历史且不被后续 publish 重复
            for rec in list(self._buffer):
                q.put_nowait(rec)
            self._subs.add((q, loop))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """SSE 连接断开时调用，防止队列与 loop 引用泄漏。"""
        with self._lock:
            self._subs = {(sq, l) for (sq, l) in self._subs if sq is not q}

    def publish(self, record: dict) -> None:
        """可被任意线程调用：写缓冲，并向每个订阅者的事件循环投递。

        Why call_soon_threadsafe：asyncio.Queue.put_nowait 若在工作线程直接调用，
        会与事件循环线程的 await q.get() 产生数据竞争；call_soon_threadsafe 把
        投递动作排进目标 loop 的就绪队列，由该 loop 单线程串行执行，彻底消除竞态。
        """
        with self._lock:
            self._buffer.append(record)
            subs = list(self._subs)  # 快照后释放锁，缩短临界区
        for (q, loop) in subs:
            try:
                loop.call_soon_threadsafe(_safe_put, q, record)
            except RuntimeError:
                # 该订阅者事件循环已关闭（SSE 客户端断开 / 连接半断）→ 立即清理，
                # 否则死订阅者永久滞留 _subs：此后每条日志都会对其触发一次无效的
                # call_soon_threadsafe + RuntimeError，且 _subs 单调增长，长期运行
                # 必然内存与开销泄漏。
                # 跨线程纪律：call_soon_threadsafe 已在锁外调用，此处仅持锁操作
                # 纯 Python 对象（重建 set）；与 unsubscribe 一致用 `is` 定位、
                # 重建 set 规避“迭代中修改”问题。注意按 q 比对（同一 loop 上可能
                # 有多个队列，但每个 (q, loop) 唯一，按 q 移除即可精准剔除该死订阅者）。
                with self._lock:
                    self._subs = {
                        (sq, sl) for (sq, sl) in self._subs if sq is not q
                    }


def _safe_put(q: asyncio.Queue, rec: dict) -> None:
    """在订阅者事件循环内执行：满则丢新条目，绝不阻塞事件循环。

    Why QueueFull 静默丢弃：订阅端消费慢导致队列打满时，宁可丢日志也不能阻塞
    事件循环——后者会拖垮整个 SSE 服务甚至回测 API。
    """
    try:
        q.put_nowait(rec)
    except asyncio.QueueFull:
        pass


class RingBufferLogHandler(logging.Handler):
    """把 Python logging 记录转成 dict 喂给 LogStreamHub。

    挂到 root logger 后，业务线程（含 run_in_threadpool 里的回测）所有日志
    都会经此 handler 进入 SSE 管线。
    """

    def __init__(self, hub: LogStreamHub, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # 有 formatter 用 formatter（如 "name | message"），否则取原始消息
            message = self.format(record) if self.formatter else record.getMessage()
            self._hub.publish(
                {
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                }
            )
        except Exception:
            # 遵循 logging 契约：自身出错交由 handleError 处理，绝不抛出打断业务
            self.handleError(record)


# 模块级单例（main.py lifespan 挂到 root logger）
log_stream_hub = LogStreamHub()


@router.get("/stream", summary="实时日志 SSE 流")
async def stream_logs() -> StreamingResponse:
    """SSE：每条日志为一帧 `data: {json}\\n\\n`。客户端用 EventSource 订阅。

    生命周期：订阅在 endpoint 进入时建立，客户端断开（生成器被取消）时 finally
    注销队列，杜绝泄漏。
    """

    async def event_gen():
        q = log_stream_hub.subscribe()
        try:
            while True:
                record = await q.get()
                # SSE 序列化统一出口（allow_nan=False）：日志帧若意外含 NaN/不可序列化
                # 则跳过，绝不崩日志流（丢一条日志优于断整条流）
                frame = sse_dumps(record, logger)
                if frame is None:
                    continue
                yield frame
        finally:
            log_stream_hub.unsubscribe(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")
