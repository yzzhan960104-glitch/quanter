# -*- coding: utf-8 -*-
"""
单资产回测路由

职责：
1. 定义 POST /api/v1/backtest/run 端点
2. 接收请求，Pydantic 自动校验参数
3. 调用 backtest_service 执行回测
4. 捕获异常并转为 HTTPException

设计原则：
- 路由层只做参数校验 + 异常捕获 + 响应格式化
- 业务逻辑全部委托给 service 层
- 异常信息使用中文，便于前端直接展示

性能红线：
- run_single_backtest() 是 CPU 密集型同步函数
- 绝对禁止在 async def 中直接调用（会阻塞事件循环）
- 必须通过 run_in_threadpool 卸载到独立线程
"""
import asyncio
import json
import traceback
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from server.schemas.backtest import BacktestRequest, BacktestResponse
from server.services.backtest_service import run_single_backtest
from server.core.config import API_CONFIG

router = APIRouter(prefix="/backtest", tags=["单资产回测"])


@router.post(
    "/run",
    response_model=BacktestResponse,
    summary="执行单资产回测",
    description=(
        "接收回测参数，执行完整回测流程（数据获取 → 因子计算 → 信号融合 → 回测执行），"
        "返回绩效指标、净值时序、回撤时序和交易记录。"
    ),
)
async def run_backtest(req: BacktestRequest) -> BacktestResponse:
    """
    执行单资产回测

    ── 事件循环保护 ──
    run_single_backtest() 包含 CPU 密集的逐日回测循环，
    若在 async def 中直接同步调用，会阻塞 FastAPI 的 asyncio 事件循环，
    导致所有并发请求排队等待（包括 /health 等轻量端点）。

    解决方案：通过 run_in_threadpool 将 CPU 密集任务卸载到线程池，
    事件循环在线程执行期间可继续处理其他请求。

    异常处理策略：
    - Pydantic 校验失败 → 自动返回 422（FastAPI 内建行为）
    - 引擎内部异常（NaN/Inf/数据不足）→ 500 + 中文错误信息
    - 回测超时 → 504
    """
    try:
        # 【性能红线】CPU 密集任务必须卸载到线程池，绝不可同步调用
        result = await run_in_threadpool(run_single_backtest, req)
        return result
    except ValueError as e:
        # 数据/因子/引擎的参数异常
        raise HTTPException(
            status_code=500,
            detail=f"回测执行异常: {str(e)}"
        )
    except KeyError as e:
        # DataFrame 列缺失等数据结构异常
        raise HTTPException(
            status_code=500,
            detail=f"数据结构异常，缺失字段: {str(e)}"
        )
    except Exception as e:
        # 兜底：未知异常
        tb = traceback.format_exc()
        raise HTTPException(
            status_code=500,
            detail=f"回测执行未知异常: {str(e)}\n{tb}"
        )


# ============ per-run SSE 流（EventSource 只支持 GET，故两步式）============
# Why 两步式：浏览器原生 EventSource 仅支持 GET 方法，无法在请求体里携带回测
# 参数。故拆为：POST /run/async 提交参数、领取 run_id；GET /run/stream/{run_id}
# 用 EventSource 订阅该 run 的事件流。run_id 在进程内 _run_registry 串联两次请求。
#
# Why 进程内注册表（非 Redis）：单进程 uvicorn + 单 worker 场景下足够；多
# worker 部署需替换为共享存储（Redis/DB），属后续运维迭代，本期不预埋抽象。
_run_registry: dict[str, BacktestRequest] = {}


class _RunBridge:
    """单订阅者跨线程桥：业务线程 publish，事件循环消费。

    Why call_soon_threadsafe：回测跑在线程池（run_in_threadpool），emitter
    在工作线程被调用；asyncio.Queue 非线程安全，若在工作线程直接 put_nowait
    会与事件循环线程的 await q.get() 产生数据竞争，破坏队列内部状态。必须用
    loop.call_soon_threadsafe 把 put 投递动作排入消费端事件循环，由该 loop
    单线程串行执行，彻底消除竞态（复用 server/api/v1/logs.py LogStreamHub
    已验证的模式）。

    Why QueueFull 静默丢弃：消费端慢导致队列打满时，宁可丢事件也不能阻塞
    事件循环——后者会拖垮整个 SSE 服务甚至其它回测 API。maxsize=2000 是
    对"单 run 正常进度/成交事件总量"的合理上界估计。
    """

    def __init__(self) -> None:
        # 单订阅者：每个 GET /run/stream 请求新建一个 bridge，无需 fan-out
        self._q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        # 捕获消费端事件循环：__init__ 在 async 端点里调用，拿到的即消费端 loop
        self._loop = asyncio.get_running_loop()

    def publish(self, ev: dict) -> None:
        """可被任意线程调用：把事件投递到消费端事件循环。

        Why try/except RuntimeError：客户端断开后事件循环关闭，
        call_soon_threadsafe 会抛 RuntimeError。此处绝不抛——保护引擎回测
        循环不被回调异常打断（T13 审查 Minor #3：emitter 永不成为引擎崩溃源）。
        """
        try:
            self._loop.call_soon_threadsafe(self._safe_put, ev)
        except RuntimeError:
            # 消费端 loop 已关闭（客户端断开）→ 静默，事件丢弃即可
            pass

    def _safe_put(self, ev: dict) -> None:
        """在消费端事件循环内执行：满则丢，绝不阻塞事件循环。"""
        try:
            self._q.put_nowait(ev)
        except asyncio.QueueFull:
            # 队列打满 → 丢新事件，保证事件循环不被阻塞（与 logs._safe_put 同语义）
            pass

    async def get(self) -> dict:
        """消费端 await 下一帧事件（阻塞直到 publish 投递）。"""
        return await self._q.get()


def _run_with_emitter(req: BacktestRequest, publish) -> dict:
    """线程池内执行回测：尝试把 emitter 透传给 service/engine。

    ── emitter 透传现状（截至 T14）──
    engine.run（单资产）已在 T13 支持 event_emitter 关键字（progress/trade/risk）；
    但 service 层走的是 engine.run_portfolio（组合路径），该方法签名尚未支持
    event_emitter（需在组合回测循环里布点 emit + 配套测试，属后续引擎扩展）。

    故此处用 try/except TypeError 兜底：
    - 若未来 service.run_single_backtest 加 event_emitter 关键字并能透传到
      engine.run_portfolio，则中途会推送 progress/trade 帧（最佳体验）；
    - 当前 service 不接受该关键字 → 抛 TypeError → 走 except 按原签名调用，
      流仍能正常返回最终 result，只是中途无 progress/trade 帧（可接受）。
    """
    try:
        return run_single_backtest(req, event_emitter=publish)
    except TypeError:
        # service 暂不支持 event_emitter 关键字 → 兜底原签名，流仍返回最终 result
        return run_single_backtest(req)


@router.post("/run/async", summary="创建回测 run（返回 run_id）")
async def create_run(req: BacktestRequest):
    """提交回测参数，领取 run_id（前端凭此拼 GET /run/stream/{run_id}）。

    Why 仅注册不执行：执行放到 GET 端点的事件流里触发，保证订阅先建立、
    事件不丢首帧；POST 端点保持轻量、即时返回，便于前端拿到 run_id 后再
    用 EventSource 发起 GET。
    """
    run_id = str(uuid.uuid4())
    _run_registry[run_id] = req
    return {"run_id": run_id}


@router.get("/run/stream/{run_id}", summary="回测 per-run SSE 流")
async def stream_run(run_id: str):
    """SSE 流：逐帧推送 progress/trade/result/error，末帧 [DONE]。

    Why 404 而非空流：run_id 未命中注册表时若返空 yield，客户端会挂起空流；
    强制 404 让前端立即感知"run 不存在/已过期"，避免幽灵连接。

    ── 协程安全纪律 ──
    - 客户端断开 → 生成器被取消 → finally 清理 _run_registry（防注册表泄漏）。
    - bridge.publish 绝不抛（RuntimeError/QueueFull 均静默），保护引擎循环。
    - 后台 runner 用 asyncio.create_task 启动，与 gen() 解耦；gen() 退出时
      runner 若仍在跑会被孤立，但引擎是 CPU 密集同步循环，run_in_threadpool
      池线程会跑完，结果 publish 到已关闭 loop 时静默丢弃，无积压无泄漏。
    """
    req = _run_registry.get(run_id)
    if req is None:
        # 未知/过期 run → 404，防客户端挂起空流
        raise HTTPException(status_code=404, detail="run 不存在或已过期")

    bridge = _RunBridge()

    async def gen():
        # 后台在线程池跑回测，结束后 publish result/error
        async def runner():
            try:
                result = await run_in_threadpool(
                    _run_with_emitter, req, bridge.publish
                )
            except Exception as e:
                # 引擎异常 → 推 error 帧，前端展示中文错误后断流
                bridge.publish({"type": "error", "message": str(e)})
                return
            # 成功 → 推 result 帧（BacktestResponse 经 json 序列化）
            bridge.publish({"type": "result", "data": result})
        asyncio.create_task(runner())
        try:
            while True:
                ev = await bridge.get()
                # default=str：兜底 BacktestResponse pydantic 实例与 numpy 标量
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                # result / error 为终态帧，收到即收尾
                if ev.get("type") in ("result", "error"):
                    break
            # SSE 终止哨兵：前端据此关闭 EventSource 连接
            yield "data: [DONE]\n\n"
        finally:
            # 防注册表泄漏：客户端断开或正常收尾都必清理（单消费 run 用后即弃）
            _run_registry.pop(run_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream")
