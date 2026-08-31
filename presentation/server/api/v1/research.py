# -*- coding: utf-8 -*-
"""Phase C 研究提案 API（2026-08-03 · Agent 长周期交互的 HTTP 载体）。

端点：
    GET    /research/proposals              提案列表（可按 status 过滤）

历史（2026-08-31 写端点全量退役）：POST generate/review/verify/publish 四端点
已删——提案引擎的驱动入口只剩非 HTTP 链路：research.digest cron（18:30 直调
generate/verify/publish）、discovery_bridge auto_publish、experiment autopromote
CLI。钉钉 @ 回复人审入口随 dingtalk_review_bridge 桥一并退役。
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter

from research import proposals

router = APIRouter(prefix="/research", tags=["研究提案"])


@router.get("/proposals")
def list_proposals(status: Optional[str] = None) -> Dict[str, Any]:
    """提案列表（created_at 降序；可按状态过滤）。"""
    return {"proposals": proposals.list_proposals(_db(), status=status)}


def _db() -> str:
    """提案库路径（env 可覆盖，测试 monkeypatch proposals._DEFAULT_DB 生效）。"""
    import os
    return os.environ.get("RESEARCH_PROPOSALS_DB", proposals._DEFAULT_DB)
