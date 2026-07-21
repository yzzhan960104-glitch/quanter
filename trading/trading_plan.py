# -*- coding: utf-8 -*-
"""T-1 交易计划（JSON 落盘 + 人工确认闸 + 钉钉推送）。

二期引擎 T-1 确认闸（spec 红线）物理意图：
    eod_plan（T-1 晚 15:35）扫信号生成 orders
    → save_plan（confirmed=false）落盘
    → push_plan_to_dingtalk 推交易机器人群（研究员人审）
    → 研究员钉钉回复「确认」
    → confirm_plan（confirmed=true）
    → 次日 pre_open（09:22）**检查 confirmed 才挂单**，未确认不挂任何单。

为什么需要确认闸：机器自动扫信号可能因数据瑕疵/前视偏差/极端行情误判，
T-1 晚给人一次否决机会，防止机器盲发导致不可逆的实盘敞口。

orders 采用嵌套格式（与 Task 9 engine.eod_plan 生产侧、本模块 push_plan_to_dingtalk
消费侧全链路统一）：
    {"order": {symbol/qty/side/price}, "stop_price": ..., "take_profit": ...}
save_plan 是 JSON 透传，不关心内部结构。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from broadcast.push import push_brief

logger = logging.getLogger(__name__)


def _plan_path(date: str) -> Path:
    """T 日计划文件路径：<TRADE_PLAN_DIR>/plan_<date>.json。

    TRADE_PLAN_DIR 默认 logs/trading_plans；生产环境由调度器/启动脚本显式注入。
    """
    base = Path(os.getenv("TRADE_PLAN_DIR", "logs/trading_plans"))
    return base / f"plan_{date}.json"


def save_plan(date: str, orders: list) -> Path:
    """落盘 T 日交易计划（confirmed=false）。

    Args:
        date: 交易日（T），如 "2026-07-22"。
        orders: PlannedOrder 嵌套字典列表，JSON 透传不校验内部结构。

    Returns:
        落盘文件 Path。父目录自动创建（parents=True，支持嵌套 TRADE_PLAN_DIR）。
    """
    p = _plan_path(date)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": date, "confirmed": False, "orders": orders}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("T-1 计划已落盘 %s（%d 单，待确认）", p, len(orders))
    return p


def load_plan(date: str) -> dict | None:
    """读计划。不存在返 None；损坏（非法 JSON/IO 错误）也返 None 不抛。

    返回 None 的物理含义：pre_open 检查时据此跳过挂单——宁可漏挂，不挂脏计划。
    """
    p = _plan_path(date)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # 损坏计划：记录完整堆栈供排查，但返 None 让 pre_open 安全降级（不挂单）。
        logger.exception("计划损坏 %s", p)
        return None


def confirm_plan(date: str) -> bool:
    """标记 T 日计划为已确认（人工钉钉确认后调）。

    幂等：重复确认返 True，不会改写 orders。

    Returns:
        True 计划存在并已置 confirmed=true；False 计划不存在（防幻觉确认）。
    """
    plan = load_plan(date)
    if plan is None:
        # 计划不存在绝不能仅凭一次调用就置确认位，否则 pre_open 会挂空计划。
        return False
    plan["confirmed"] = True
    _plan_path(date).write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("计划 %s 已确认", date)
    return True


def push_plan_to_dingtalk(date: str, orders: list) -> bool:
    """把 T-1 计划推到交易机器人群（研究员钉钉确认用）。

    复用一期 broadcast.push.push_brief（subprocess 调 dws send-by-bot，
    零自写加签）。格式化嵌套 orders 为 Markdown。

    Args:
        date: 交易日。
        orders: 嵌套格式 list（同 save_plan）。

    Returns:
        push_brief 返回值透传：成功 True；缺凭证/超时/dws 不在/returncode≠0 → False（不抛）。
    """
    # 格式化在 try 外也可，但兜底：orders 结构异常时不抛，记堆栈返 False。
    try:
        lines = [
            f"- {o['order']['symbol']} {o['order']['side']} {o['order']['qty']}股"
            f"@{o['order']['price']}（止损{o['stop_price']}/止盈{o['take_profit']}）"
            for o in orders
        ]
        md = (
            f"### T-1 交易计划 {date}\n"
            f"> 待确认（回复「确认」即挂单）\n\n"
            + "\n".join(lines)
        )
        # 凭证从环境读：TRADING_BOT_ROBOT_CODE（交易机器人）/ BROADCAST_GROUP_ID（运营群），
        # 与一期 broadcast __main__._BOT_CFG["trading"] / _GROUP_ID_ENV 完全一致。
        robot = os.getenv("TRADING_BOT_ROBOT_CODE", "")
        group = os.getenv("BROADCAST_GROUP_ID", "")
        return push_brief(
            f"交易计划 {date}", md, robot_code=robot, group_id=group
        )
    except Exception:
        # orders 结构漂移/缺 key → 记堆栈返 False，绝不抛到调度器致 cron 整体崩。
        logger.exception("推计划到钉钉失败")
        return False
