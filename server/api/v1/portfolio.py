# -*- coding: utf-8 -*-
"""
组合回测路由

职责：
1. 定义 POST /api/v1/portfolio/run 端点
2. 接收组合回测参数（多标的、HMM 配置、迟滞阈值）
3. 调用 portfolio_service 执行组合回测
4. 捕获异常并转为 HTTPException

设计原则：
- 组合回测比单资产更复杂（HMM 训练 + 状态映射 + 迟滞滤波）
- 超时时间更长（默认 120 秒）
- HMM 训练失败时返回明确的错误信息
"""
import traceback

from fastapi import APIRouter, HTTPException

from server.schemas.portfolio import PortfolioRequest, PortfolioResponse
from server.services.portfolio_service import run_portfolio_backtest
from server.core.config import API_CONFIG

router = APIRouter(prefix="/portfolio", tags=["组合回测"])


@router.post(
    "/run",
    response_model=PortfolioResponse,
    summary="执行组合回测",
    description=(
        "接收组合回测参数，执行完整流程："
        "数据获取 → HMM 训练 → 状态概率预测 → 迟滞滤波 → 组合调仓回测。"
        "返回绩效指标、净值时序、回撤时序、权重时序和交易记录。"
    ),
)
async def run_portfolio(req: PortfolioRequest) -> PortfolioResponse:
    """
    执行组合回测

    异常处理策略：
    - Pydantic 校验失败 → 422
    - HMM 训练失败（数据不足/收敛失败）→ 500
    - 引擎内部异常 → 500
    - 超时 → 504
    """
    try:
        result = run_portfolio_backtest(req)
        return result
    except ValueError as e:
        # HMM 训练/映射参数异常
        raise HTTPException(
            status_code=500,
            detail=f"组合回测执行异常: {str(e)}"
        )
    except RuntimeError as e:
        # HMM 模型未训练等运行时异常
        raise HTTPException(
            status_code=500,
            detail=f"HMM 模型异常: {str(e)}"
        )
    except KeyError as e:
        # 数据结构异常
        raise HTTPException(
            status_code=500,
            detail=f"数据结构异常，缺失字段: {str(e)}"
        )
    except Exception as e:
        # 兜底：未知异常
        tb = traceback.format_exc()
        raise HTTPException(
            status_code=500,
            detail=f"组合回测未知异常: {str(e)}\n{tb}"
        )
