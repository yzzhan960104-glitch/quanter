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
import traceback

from fastapi import APIRouter, HTTPException
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
