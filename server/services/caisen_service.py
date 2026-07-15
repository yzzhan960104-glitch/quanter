# -*- coding: utf-8 -*-
"""蔡森形态学流水线 server 层编排服务（Phase 3 · Step2 降级为 facade 薄壳）。

Step2 重构后：本模块不再穿透 caisen 内部 8 个子模块（plan/storage/
backtest_replay/replay_runs/replay_tasks_db/patterns/risk/config），
改为持有 CaisenFacade 单例并转发 10 个用例。server/api/v1/caisen.py
调用点零改动（模块级函数名与签名不变）。

异常契约不变：ValidationError/ValueError/KeyError 透传路由层转 422/404
（facade 已保证不吞，本薄壳仅转发，不额外捕获）。
"""
from __future__ import annotations

from typing import List, Optional

from caisen.facade import CaisenFacade
from server.schemas.caisen import (
    CandidatePlan, PlanReview, ReplayReportResponse, ReplayRequest,
    ReplayRunDetail, ReplayRunSummary, ScanRequest,
)

# facade 单例：模型层唯一对外契约，内部重组对本薄壳不可见
_facade = CaisenFacade()


def run_scan(req: ScanRequest) -> List[CandidatePlan]:
    return _facade.scan(req)


def list_plans(status: Optional[str] = None) -> List[CandidatePlan]:
    return _facade.list_plans(status)


def approve_plan(plan_id: str, review: PlanReview) -> CandidatePlan:
    return _facade.approve_plan(plan_id, review)


def activate_plan(plan_id: str) -> CandidatePlan:
    return _facade.activate_plan(plan_id)


def get_plan(plan_id: str) -> Optional[CandidatePlan]:
    return _facade.get_plan(plan_id)


def run_replay(req: ReplayRequest) -> ReplayReportResponse:
    return _facade.replay(req)


def run_replay_async(req) -> str:
    return _facade.replay_async(req)


def list_replay_runs() -> List[ReplayRunSummary]:
    return _facade.list_replay_runs()


def get_replay_run(run_id: str) -> Optional[ReplayRunDetail]:
    return _facade.get_replay_run(run_id)


def delete_replay_run(run_id: str) -> bool:
    return _facade.delete_replay_run(run_id)


# ── 兼容转发（strangler 过渡期模块级名字）──────────────────────────────────
# replay_worker.py 模块级 `from server.services.caisen_service import
# _load_price_data, _merge_cfg` 把这两个名字绑成 replay_worker 模块属性
# （其测试 monkeypatch replay_worker._load_price_data 依赖此模块级名字语义）。
# 编排逻辑已收口到 facade（实例方法），此处仅转发 facade 单例的对应方法，
# 让 replay_worker 的 import 继续可用。Step3 replay_worker 迁 caisen/infra 时
# 改依赖 facade，届时删此转发。
# 注：facade 的 _load_price_data/_merge_cfg 是实例方法（首个参数 self），
# 此处用 `_facade._load_price_data(symbols, date)` 调用已绑定实例的方法，
# 签名对外呈现 (symbols, date) / (cfg_override)，与旧模块级函数签名一致。
def _load_price_data(symbols, date):
    return _facade._load_price_data(symbols, date)


def _merge_cfg(cfg_override):
    return _facade._merge_cfg(cfg_override)
