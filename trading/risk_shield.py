"""
trading/risk_shield.py
======================
下单风控挡板（纯函数，无 I/O）。

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
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from trading.execution_gateway import OrderRequest


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
    allow_live: bool,
    whitelist: set,
    max_amount: float,
    max_shares: float,
    quote: Mapping[str, Any] | None,
    enforce_session: bool,
    is_locked: bool,
    connected: bool,
    confirm: bool,
    in_session: bool = True,
) -> RiskDecision:
    """10 关短路校验。任一关命中即返 RiskDecision(blocked=True, stage=<关卡名>)。

    关卡顺序即优先级（短路）：
      1 connection  断线/未连接          — 状态机边界，最高优先
      2 dry_run     请求级模拟           — is_dry_run=True，非错误
      3 allow_live  实盘总闸(env)        — 强制模拟
      4 confirm     二次确认             — 防误触
      5 whitelist   标的白名单
      6 lot         A 股 100 整手契约
      7 max_amount  单笔金额上限
      8 max_shares  单笔股数上限
      9 high/low_limit  涨跌停封板（quote 缺失则跳过）
     10 session     A 股交易时段（enforce_session=True 时生效）
    """
    # 关1：断线/连接（最高优先——断线时其他校验无意义）
    if is_locked or not connected:
        return RiskDecision(True, "网关未连接或已锁定（断线保护）", "connection")

    # 关2：dry_run（请求级，前端控制）—— 模拟语义，is_dry_run=True
    if dry_run:
        return RiskDecision(True, "dry_run 模拟（前端请求不真下单）", "dry_run", is_dry_run=True)

    # 关3：实盘总闸（env QMT_ALLOW_LIVE_TRADE）
    if not allow_live:
        return RiskDecision(True, "实盘总闸 QMT_ALLOW_LIVE_TRADE=false，禁止真下单", "allow_live")

    # 关4：二次确认
    if not confirm:
        return RiskDecision(True, "缺少二次确认 confirm=true", "confirm")

    # 关5：标的白名单
    if order.symbol not in whitelist:
        return RiskDecision(True, f"标的 {order.symbol} 不在白名单", "whitelist")

    # 关6：A 股 100 整手契约（qty<=0 或非 100 整数倍 → 拒）
    if order.qty <= 0 or int(order.qty) % 100 != 0:
        return RiskDecision(True, f"数量 {order.qty} 非 100 整数倍（A 股整手契约）", "lot")

    # 关7：单笔金额上限（限价用 order.price，市价用 quote.last_price 估算）
    ref_price = order.price
    if ref_price is None and quote is not None:
        ref_price = quote.get("last_price")
    if ref_price is not None and order.qty * ref_price > max_amount:
        return RiskDecision(
            True,
            f"单笔金额 {order.qty * ref_price:.2f} 超上限 {max_amount}",
            "max_amount",
        )

    # 关8：单笔股数上限
    if order.qty > max_shares:
        return RiskDecision(True, f"单笔股数 {order.qty} 超上限 {max_shares}", "max_shares")

    # 关9：涨跌停封板（quote 缺失 → 跳过，xtdata 不可用时的降级）
    # #6 修复：按 side 区分（A 股涨跌停物理正确性）。
    #   涨停(last≥high)：买盘封死 → 能卖不能买 → 仅 BUY 拦（买不进），SELL 放行。
    #   跌停(last≤low)：卖盘封死 → 能买不能卖 → 仅 SELL 拦（卖不出），BUY 放行。
    #   原实现不分 side：SELL 涨停被拦=止盈/止损 SELL 无法发出（错过离场=敞口失控，
    #   本关最致命的实盘风险）；BUY 跌停被拦=错过建仓。蔡森 tick_exit 的止损/止盈/
    #   时间止损全走 SELL，涨停时必须放行 SELL。stage 名沿用 high_limit/low_limit
    #   不破前端/审计契约。
    if quote is not None:
        last = quote.get("last_price")
        high = quote.get("high_limit")
        low = quote.get("low_limit")
        side = order.side.lower()
        if side == "buy" and last is not None and high is not None and last >= high:
            return RiskDecision(
                True, f"{order.symbol} 涨停封板，BUY 无法成交（{last}≥{high}）", "high_limit")
        if side == "sell" and last is not None and low is not None and last <= low:
            return RiskDecision(
                True, f"{order.symbol} 跌停封板，SELL 无法成交（{last}≤{low}）", "low_limit")

    # 关10：A 股交易时段
    if enforce_session and not in_session:
        return RiskDecision(True, "非 A 股交易时段", "session")

    # 全过：放行真下单
    return RiskDecision(False)
