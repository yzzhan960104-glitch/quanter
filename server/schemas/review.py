# -*- coding: utf-8 -*-
"""层级六·AI 复盘的 Pydantic 契约。

- ReviewRequest：复盘输入（csv_text 上传 或 start/end 按日期读日志 + 策略上下文）。
- ReviewReport：复盘输出（Markdown 报告；LLM 不可用时降级为上下文摘要）。
"""
from typing import Any, Dict, Optional

from pydantic import BaseModel


class ReviewRequest(BaseModel):
    """POST /review/diagnose 请求。

    数据源二选一：
    - csv_text：直接粘贴的实盘日志文本（CSV 格式，优先）。
    - start/end：按日期从 logs/live_trades.csv 读取（csv_text 缺省时生效）。
    策略上下文（可选，富化 Prompt）：
    - strategy_name / strategy_params / metrics（关键指标如 max_drawdown）。
    """
    csv_text: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    strategy_name: Optional[str] = None
    strategy_params: Dict[str, Any] = {}
    metrics: Dict[str, Any] = {}


class ReviewReport(BaseModel):
    """复盘报告响应。"""
    ok: bool
    report: str                           # Markdown 报告（或降级模式下的上下文摘要）
    model: Optional[str] = None           # 实际使用的模型（如 glm-4）；降级时为 None
    degraded: bool = False                # LLM 不可用（缺凭证/调用失败）→ True
    reason: Optional[str] = None          # 降级原因（degraded=True 时填）
