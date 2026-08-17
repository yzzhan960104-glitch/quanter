# -*- coding: utf-8 -*-
"""trading.compute.risk — 下单风控挡板纯函数（functional core）。

物理定位（Layer2 阶段2 · spec §3.5/§4）：
    check_order 是【纯函数】（无 I/O、无状态、确定性）——所有外部数据（quote 快照、
    连接状态、dry_run、env 配置）由调用方注入，保证可确定性穷举单测、回测与实盘
    共用同一风控判定（杀手不变量）。本模块仅依赖标准库 + trading.compute.types
    （OrderRequest 纯 dataclass），零外部依赖。

设计哲学（CLAUDE.md Karpathy 极简 + 事实审查）：
- 纯函数：所有外部数据（quote 快照、连接状态、dry_run、env 配置）由调用方注入，
  保证 test_risk_shield.py 可确定性穷举单测，无需 mock 网络/环境。
- 短路求值：10 关自上而下，任一命中即返 blocked，不继续下关（关 1 连接优先级最高）。
- 决策可审计：RiskDecision.stage 记命中关卡名，便于落 CSV + 前端分流提示。

dry_run 双开关语义（研究员明确要求"前端控制是否真实下单"）：
- dry_run（请求级，POST body）= True → 模拟，不真下单，is_dry_run=True（非错误，
  调用方应落 DRY_RUN_* 流水后返回成功语义）
- dry_run=False 但 allow_live（env QMT_ALLOW_LIVE_TRADE）=False → 拒单（强制模拟）
- dry_run=False 且 allow_live=True → 放行真下单

迁移纪律（strangler 红线①）：check_order 逻辑【零改动】，只搬位置（trading/
risk_shield.py → trading/compute/risk.py）。原 trading/risk_shield.py 留垫片
re-export ``from trading.compute.risk import check_order, RiskDecision``，既有
``from trading.risk_shield import check_order`` 调用零改动继续可用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from trading.compute.types import OrderRequest


@dataclass(frozen=True)
class RiskDecision:
    """风控挡板决策（不可变值对象）。

    blocked=True 时 reason/stage 非空。
    is_dry_run=True 仅在 dry_run 模拟命中时为真——它是「模拟」而非「错误」，
    调用方据此落 DRY_RUN_* 流水并返回成功语义（区别于其他关的 409 拒单）。
    """

    blocked: bool
    reason: str = ""
    stage: str = ""
    is_dry_run: bool = False


def check_order(
    order: OrderRequest,
    *,
    dry_run: bool,
    enforce_session: bool,
    is_locked: bool,
    connected: bool,
    in_session: bool = True,
    block_new_orders: bool = False,
) -> RiskDecision:
    """A-2 短路校验（2026-08-06 裁定 D1-D3/D5 后 3 道闸 + ADR-16 人工开关关）。

    被删闸（决策记录 Annotation 1 + 裁定）：
      allow_live / confirm / whitelist / lot / max_amount / max_shares / high_low_limit
      ——业务拦截阈值全部移除：A 股整手/涨跌停由柜台与交易所兜底，白名单前端同步放开（D3），
      二次确认由计划确认闸（T-1 人审/自动确认）承担。
    保留闸（链路正确性原语，D1/D2）：
      1 connection  断线/未连接          — 状态机边界，最高优先（D1）
      2 dry_run     请求级模拟           — is_dry_run=True，非错误
      3 master_switch 人工增量拦截       — ADR-16：block_new_orders=True 拒一切真买单
                    （只拦 side="buy"；卖出=止损/止盈/超期退出，永不拦；dry_run 模拟不受限）
      4 session     A 股交易时段（enforce_session=True 时生效；上午起点 09:15，A1 已修）
    """
    # 闸1：断线/连接（最高优先——断线时其他校验无意义）
    if is_locked or not connected:
        return RiskDecision(True, "网关未连接或已锁定（断线保护）", "connection")

    # 闸2：dry_run（请求级，前端控制）—— 模拟语义，is_dry_run=True
    if dry_run:
        return RiskDecision(True, "dry_run 模拟（前端请求不真下单）", "dry_run", is_dry_run=True)

    # 闸3：人工增量拦截开关（ADR-16 · 2026-08-17）——只拦买单，卖出（退出）永不拦。
    # Why 在 dry_run 之后：模拟请求不产生真增量，放行保链路可测试；
    # Why 只判 side=="buy"：开关语义是「拦截增量下单」，止损/止盈/超期平仓是存量退出。
    if block_new_orders and order.side == "buy":
        return RiskDecision(True, "人工风控开关：拦截增量买入（卖出/退出不拦）", "master_switch")

    # 闸4：A 股交易时段（enforce_session=True 时生效；D2 保留，09:15 起点 A1 已修）
    if enforce_session and not in_session:
        return RiskDecision(True, "非 A 股交易时段", "session")

    # 全过：放行真下单
    return RiskDecision(False)
