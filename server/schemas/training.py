# -*- coding: utf-8 -*-
"""训练 loop API 的 Pydantic 契约（Spec 3 §8）。

物理定位：本文件只定义 HTTP 层的请求/响应骨架，不含业务逻辑。
字段与 caisen.training_loops_db 的 training_loops 表对齐——
get_loop 返回的 dict 可直接被 TrainingLoopState 序列化（history 为 round 摘要 list）。
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TrainingStartRequest(BaseModel):
    """POST /training/start：提交训练 loop。

    物理意图：base_cfg_override = 提交时初始 cfg（=重置基准，每轮人审改的是
    current_cfg，base_cfg 永久保留作"回到原点"参照）；缺省字段由 StrategyConfig
    默认。start/end/universe = 每轮回测固定区间与标的池（与 replay 同语义，
    跨轮不变以保证调参对比的纯净性）。
    """
    start: str
    end: str
    universe: Optional[List[str]] = None
    base_cfg_override: Dict[str, Any] = Field(default_factory=dict)
    max_rounds: int = 20      # 默认 20（spec §11 拍板；可按算力上限配）


class RoundSummary(BaseModel):
    """单轮回测统计摘要（history_json 数组的一元素）。

    物理意图：每轮只存 ~6 个核心指标（不带完整 trades 控量），喂 GLM 做多轮
    趋势分析（如"参数越调越激进，回撤在放大"），同时供前端折线图渲染。
    """
    round: int
    n_hits: int = 0
    win_rate: float = 0.0
    avg_rr: float = 0.0
    max_dd: float = 0.0
    annualized: float = 0.0


class TrainingLoopState(BaseModel):
    """GET /training/{loop_id}：loop 状态 + 历史轮次摘要。

    字段对齐 training_loops 表列（_row_to_dict 反序列化后的形态）：
    - base_cfg/current_cfg：cfg 永远是 dict（空 → {}，非 None）。
    - history：轮次摘要数组，初始 []。
    - pending_review/error：AWAITING_REVIEW 期的待审文本 / 异常终止因（正常为 None）。
    - status：IDLE/RUNNING/ANALYZING/AWAITING_REVIEW/CONFIRMING/STOPPED/COMPLETED。
    """
    loop_id: str
    status: str
    current_round: int
    max_rounds: int
    start: Optional[str] = None
    end: Optional[str] = None
    base_cfg: Dict[str, Any] = Field(default_factory=dict)
    current_cfg: Dict[str, Any] = Field(default_factory=dict)
    history: List[RoundSummary] = Field(default_factory=list)
    pending_review: Optional[str] = None
    error: Optional[str] = None
