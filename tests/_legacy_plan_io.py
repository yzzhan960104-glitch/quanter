# -*- coding: utf-8 -*-
"""测试专用 legacy plan JSON 落盘/确认 shim（SSoT Phase C · C3 收尾）。

物理意图（C3 决策）：
    生产代码 save_plan/confirm_plan 已删（DB trade_event(SIGNAL/CONFIRMED) 是真相源，
    eod_plan 直接写）。但大量历史测试用 save_plan 做「JSON 镜像落盘」+ confirm_plan 做
    「DB CONFIRMED 写入 + JSON confirmed=true 双写」种子模式（与原 eod_plan W2 双写
    路径同构）。完全删除函数定义会破坏 11 个测试文件 ~50 处种子调用。

    决策：把原 save_plan/confirm_plan 逻辑搬到 ``tests/`` 命名空间作「测试专用 legacy shim」
    （生产 audit grep `trading presentation broadcast --glob '!tests/**'` 永远不扫到本文件），
    保留原 JSON 落盘 + DB CONFIRMED 双写语义给：
        - 测试种子（与 W2 双写时序约束对齐：SIGNAL 必须在 CONFIRMED 之前写）
        - 测试断言读 plan_*.json（trailing/归因/attribution 等老路径）
        - veto_plan.veto 单否/全否的 JSON 镜像同步（C3 改 veto 只写 DB，但 veto 测试
          仍验证 JSON 删除语义——见 test_veto_plan_db）

    生产代码永不可 import 本模块（CI 应加守卫，本 task 范围外）。

注意：
    - confirm_plan_legacy 保留 veto 保护（get_latest_action != "VETOED" 才写 CONFIRMED）。
    - confirm_plan_legacy DB 失败软降级（与 W2 同语义，人审确认不阻断）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _plan_path(date: str) -> Path:
    """T 日计划文件路径：<TRADE_PLAN_DIR>/plan_<date>.json（与 trading_plan._plan_path 同口径）。"""
    base = Path(os.getenv("TRADE_PLAN_DIR", "logs/trading_plans"))
    return base / f"plan_{date}.json"


def save_plan_legacy(date: str, orders: list, *, confirmed: bool = False) -> Path:
    """测试专用：落盘 T 日交易计划 JSON（默认 confirmed=false 待人审）。

    物理意图：原 ``trading_plan.save_plan`` 的 1:1 搬运。生产 C3 删除后，仅测试种子
    需要 JSON 镜像（断言读 plan_*.json 的老路径：trailing/归因/attribution）。
    DB SIGNAL 是真相源——种子方需另行 ``state_store.insert_trade_event(SIGNAL, ...)``。

    Returns:
        落盘文件 Path。父目录自动创建。
    """
    p = _plan_path(date)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": date, "confirmed": confirmed, "orders": orders}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def confirm_plan_legacy(date: str) -> bool:
    """测试专用：标记 T 日计划 JSON confirmed=true + 补 DB CONFIRMED（W2 双写语义）。

    物理意图：原 ``trading_plan.confirm_plan`` 的 1:1 搬运。研究員钉钉确认触发——
    JSON confirmed=true 是展示镜像，DB trade_event(CONFIRMED) 是 pre_open 据放行的真相源。

    veto 保护：``get_latest_action(trade_id) != "VETOED"`` 才写 CONFIRMED——
    研究员否决是 opt-out 终局动作，不被机器确认覆盖。

    失败语义（软降级）：DB 失败 → JSON 照写（人审确认是 opt-in 流程，DB 下次 eod 补写）。

    Returns:
        True 计划存在并已置 confirmed=true；False 计划不存在（防幻觉确认）。
    """
    from trading import state_store
    p = _plan_path(date)
    if not p.exists():
        return False
    try:
        plan = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    try:
        account_id = os.getenv("QMT_ACCOUNT_ID", state_store._DEFAULT_ACCOUNT_ID)
        state_store.init_store()
        if state_store.get_account(account_id) is None:
            state_store.upsert_account(account_id, broker="qmt")
        for o in plan.get("orders", []):
            sym = (o.get("order") or {}).get("symbol")
            if not sym:
                continue
            trade_id = state_store.build_trade_id(account_id, sym, date)
            if state_store.get_latest_action(trade_id) != "VETOED":
                state_store.insert_trade_event(account_id, trade_id, sym, "CONFIRMED")
    except Exception:
        logger.exception("confirm_plan_legacy DB 写 CONFIRMED 失败（软降级，JSON 照写）")
    plan["confirmed"] = True
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def confirm_plan_db_only(date: str) -> int:
    """测试专用：从 DB SIGNAL 反推 symbols，写 DB CONFIRMED（C3 eod_plan 流程不再落 JSON）。

    物理意图（C3 e2e 测试专用）：C3 删 save_plan 后，eod_plan 不再写 JSON 镜像——
    confirm_plan_legacy（依赖 JSON）不适用。本函数直读 DB list_signals_with_meta_by_plan_date
    拿 symbols，逐个写 CONFIRMED（与 eod_plan auto_confirm 路径同构，仅测试种子用）。

    veto 保护：``get_latest_action(trade_id) != "VETOED"`` 才写 CONFIRMED。

    Returns:
        写入 CONFIRMED 的 symbol 数（0 = 无 SIGNAL）。
    """
    from trading import state_store
    account_id = os.getenv("QMT_ACCOUNT_ID", state_store._DEFAULT_ACCOUNT_ID)
    state_store.init_store()
    if state_store.get_account(account_id) is None:
        state_store.upsert_account(account_id, broker="qmt")
    metas = state_store.list_signals_with_meta_by_plan_date(date)
    n = 0
    for m in metas:
        sym = (m.get("order") or {}).get("symbol", m.get("symbol"))
        if not sym:
            continue
        trade_id = state_store.build_trade_id(account_id, sym, date)
        if state_store.get_latest_action(trade_id) != "VETOED":
            state_store.insert_trade_event(account_id, trade_id, sym, "CONFIRMED")
            n += 1
    return n
