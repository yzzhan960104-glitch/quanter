# -*- coding: utf-8 -*-
"""训练 loop API 路由（Spec 3 §8）。

端点：
- POST /training/start           提交训练 {start,end,universe,base_cfg_override,max_rounds} → loop_id
- GET  /training/{loop_id}       loop 状态 + 历史轮次摘要
- POST /training/{loop_id}/stop  停止 loop（解除 AWAITING_REVIEW 阻塞 + 落 STOPPED）
- GET  /training                 loop 列表（created_at 降序）

设计取舍：
- 钉钉审核回调走进程内 event 唤醒（training_dingtalk 不经 HTTP），故无"提交审核"端点。
- orchestrator 经 request.app.state.training_orchestrator 取（main.py lifespan 装配，
  Task7 完成）；本 task 测试通过直接给 app.state 注 fake orchestrator 验证。
- start/stop 必须经 orchestrator（涉及并发守卫/线程 event 唤醒，DB 直写会漏掉 event
  set 导致 daemon 线程继续阻塞）；get/list 纯读，直接调 training_loops_db 更简单，
  不必绕一层 orchestrator.get_state/list_loops 转发。
"""
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from caisen import training_loops_db
from caisen.training_loop import LoopBusyError
from server.schemas.training import TrainingLoopState, TrainingReviewRequest, TrainingStartRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/training", tags=["训练 loop"])


def _orch(request: Request):
    """从 app.state 取 orchestrator（lifespan 装配）；缺 → 503。

    物理意图：Task7 的 lifespan startup 构造 TrainingLoopOrchestrator 并起 daemon，
    挂到 app.state.training_orchestrator。若该属性缺失（lifespan 未起/测试未注入），
    start/stop 无法工作，返 503 让前端明确感知"训练设施未就绪"而非静默 500。
    """
    orch = getattr(request.app.state, "training_orchestrator", None)
    if orch is None:
        raise HTTPException(503, "训练 loop 编排器未装配（lifespan 未起）")
    return orch


@router.post("/start", summary="提交训练 loop（连续 回测→AI分析→钉钉人审→调参续跑）")
def start_training(body: TrainingStartRequest, request: Request) -> Dict[str, Any]:
    """提交训练 loop。concurrency=1 守卫：已有活跃 loop → LoopBusyError → 422。

    422（而非 409）语义：业务规则冲突（一次只允许一个活跃 loop），非资源冲突。
    """
    try:
        loop_id = _orch(request).start({
            "start": body.start, "end": body.end, "universe": body.universe,
            "base_cfg": body.base_cfg_override, "max_rounds": body.max_rounds,
        })
    except LoopBusyError as exc:
        raise HTTPException(422, str(exc))
    return {"loop_id": loop_id}


@router.post("/review", summary="提交人审（dws bridge 转发钉钉@消息→submit_review）")
def submit_review(body: TrainingReviewRequest, request: Request) -> Dict[str, Any]:
    """dws dev connect 收到统一应用@消息 → bridge脚本HTTP转发 → orchestrator.submit_review。

    物理定位：审查应用是「统一应用」，老 dingtalk-stream SDK 收不到@（根因：代际不匹配），
    故改走 dws 桥：dws 用统一应用新机制收@（已实测通）→ scripts/dingtalk_review_bridge.py
    转发到此端点。端点取当前活跃 loop（concurrency=1），无活跃 → 409。submit_review
    唤醒 AWAITING_REVIEW，loop 后续 parse+回显+续跑，推送走 webhook（DingTalkNotifier）。
    """
    orch = _orch(request)
    loop_id = orch.active_loop_id
    if not loop_id:
        raise HTTPException(409, "当前无活跃训练 loop（无可审核对象）")
    orch.submit_review(loop_id, body.text)
    return {"loop_id": loop_id, "received": True}


@router.get("/{loop_id}", summary="loop 状态 + 历史轮次摘要", response_model=TrainingLoopState)
def get_training(loop_id: str) -> Any:
    """直调 training_loops_db.get_loop（纯读，不经 orchestrator）。

    response_model=TrainingLoopState 锁定传输契约：_row_to_dict 返回的原始 dict
    （含所有 DB 列）经 Pydantic 过滤，只下发 schema 声明的字段——杜绝未来加列
    自动泄漏到 JSON。返回 dict 字段对齐 TrainingLoopState（_row_to_dict 已反序列化
    cfg/history）。不存在 → 404。
    """
    loop = training_loops_db.get_loop(loop_id)
    if loop is None:
        raise HTTPException(404, f"loop {loop_id} 不存在")
    return loop


@router.post("/{loop_id}/stop", summary="停止 loop")
def stop_training(loop_id: str, request: Request) -> Dict[str, Any]:
    """人工喊停 → orchestrator.stop（落 STOPPED + set event 解除 daemon 阻塞）。"""
    _orch(request).stop(loop_id)
    return {"loop_id": loop_id, "status": "STOPPED"}


@router.get("", summary="loop 列表（created_at 降序）", response_model=List[TrainingLoopState])
def list_trainings(limit: int = 100) -> List[Any]:
    """直调 training_loops_db.list_loops（纯读，不经 orchestrator）。

    response_model=List[TrainingLoopState] 同 get：每个 row 经 Pydantic 过滤，
    传输字段集与 schema 锁定一致。
    """
    return training_loops_db.list_loops(limit=limit)
