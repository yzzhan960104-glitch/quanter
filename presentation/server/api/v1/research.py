# -*- coding: utf-8 -*-
"""Phase C 研究提案 API（2026-08-03 · Agent 长周期交互的 HTTP 载体）。

端点：
    GET    /research/proposals              提案列表（可按 status 过滤）
    POST   /research/proposals/generate     触发 Agent（LLM）生成提案
    POST   /research/proposals/review       钉钉审核入口（通过/否决 p_xxx）
    POST   /research/proposals/{id}/verify  A 档自动验证（基线 vs 提案）
    POST   /research/proposals/{id}/publish APPROVED → experiment DRAFT
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from research import proposals

router = APIRouter(prefix="/research", tags=["研究提案"])


class GenerateRequest(BaseModel):
    """提案生成请求：研究摘要 + 最近提案历史（防重复）。"""
    digest: str = ""
    history: list = Field(default_factory=list)


class ReviewRequest(BaseModel):
    """钉钉审核请求（bridge 转发 @文本）。"""
    text: str


@router.get("/proposals")
def list_proposals(status: Optional[str] = None) -> Dict[str, Any]:
    """提案列表（created_at 降序；可按状态过滤）。"""
    return {"proposals": proposals.list_proposals(_db(), status=status)}


@router.post("/proposals/generate")
def generate_proposal(body: GenerateRequest) -> Dict[str, Any]:
    """触发 Agent 生成一条提案（LLM + schema 护栏；GLM 不可用 → proposal_id=None）。"""
    pid = proposals.generate_proposal(_db(), body.digest, body.history)
    return {"proposal_id": pid}


@router.post("/proposals/review")
def review_proposal(body: ReviewRequest) -> Dict[str, Any]:
    """钉钉审核：parse（通过/否决 p_xxx）→ 状态迁移 → 回显结果。"""
    return proposals.submit_review(_db(), body.text)


@router.post("/proposals/{proposal_id}/verify")
def verify_proposal(proposal_id: str, lake_start: str = "2025-01-01") -> Dict[str, Any]:
    """A 档自动验证（基线 ACTIVE vs 提案 params）。"""
    ok = proposals.verify_proposal(_db(), proposal_id, lake_start=lake_start)
    status = proposals.get_proposal(_db(), proposal_id)["status"]
    return {"ok": ok, "status": status}


@router.post("/proposals/{proposal_id}/publish")
def publish_proposal(proposal_id: str) -> Dict[str, Any]:
    """APPROVED → experiment DRAFT（promote 仍留人审）。"""
    exp_id = proposals.publish_proposal(_db(), proposal_id)
    return {"experiment_id": exp_id}


def _db() -> str:
    """提案库路径（env 可覆盖，测试 monkeypatch proposals._DEFAULT_DB 生效）。"""
    import os
    return os.environ.get("RESEARCH_PROPOSALS_DB", proposals._DEFAULT_DB)
