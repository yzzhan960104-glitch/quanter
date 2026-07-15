# -*- coding: utf-8 -*-
"""ExecutionEngine：ARMED→FILLED→CLOSED 状态机 + 离场纯函数（Phase 3 · Task 2）。

（待迁·Step4 移出 caisen 包至执行层）本模块当前物理位于 caisen/infra/ 过渡子包，
Step4 将连同 storage/replay_*/viz_* 整体迁出 caisen 包至独立的执行编排层。当前位置
仅为 Step3 分层重构的中间态。

物理定位（CLAUDE.md 极简 + 显式 + 无黑盒原则）：
    本模块是蔡森形态学流水线 Phase 3 的"盘中执行编排层"——A 股无原生 OCO 条件单
    （One-Cancels-Other），自建状态机：ARMED（待回踩）→ FILLED（已持仓）→ CLOSED
    （已平仓）。离场条件并联判定（止损/止盈/时间止损/移动止盈），由盘中 beat 周期
    调用 tick_pullback / tick_exit 驱动。

    ── 离场纯函数 check_exit（核心设计：杜绝双源真理）──
    check_exit 是无 I/O 纯函数：输入 (pos, bar, bars_held, cfg)，输出 ExitDecision。
    Phase 2 Task10 回放验证器（backtest_replay）与实盘 ExecutionEngine 共用此函数，
    杜绝"回放一套离场规则 / 实盘另一套"的双源真理隐患。

状态机三态迁移（与 caisen/storage.py update_plan 联动）：
    ARMED   : 计划已审核通过、待盘中回踩触发（tick_pullback 监控）
              → 触及回踩区间 → submit_order(buy, price=entry_upper) → FILLED
    FILLED  : 已成交持仓（tick_exit 监控离场条件）
              → check_exit 命中 CLOSE → submit_order(sell, 市价) → CLOSED
              → 移动止盈 new_stop != None → update_plan(stop=new_stop)（止损只上移）
    CLOSED  : 已平仓（active.json 移除，持仓了结）

防御性边界（CLAUDE.md 量化风控·边界审查 · 拷问三连）：
    - 流动性与极端行情：断线不补发——tick 遇 trading.get_status() locked 或 not connected
      直接 return（不查行情、不下单），等下一轮重连。避免在不可用状态发废单/误判离场。
    - 接口与状态机边界：submit_order 复用 trading_service（过 check_order 10 关风控 +
      EMT 网关），不在执行器层另造下单逻辑；update_plan 不存在抛 KeyError（storage 守）。
    - 部分成交/网络异常：tick 内 try/except 包裹单计划异常（不中断本轮其它计划），
      单计划失败不影响整体（边界审查·边界隔离）。

注：check_exit 离场优先级为 止损 > take_profit_2x(第二波) > take_profit(第一波) > 时间止损，
与回放验证器（backtest_replay._simulate_one_trade）逐字对齐（#16 修复：消除双源真理）。
蔡森原著止损优先于止盈——防日内闪崩穿止损后反弹的假象。第二波优先于第一波：触及 2x
按更优离场档判定（回测同口径按 2x 价记 rr）。pos 缺 take_profit_2x 时降级只看第一波。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from caisen import storage

# 模块级 logger（实盘可观测性：tick 编排内单计划异常需落日志，便于运维定位
# "为什么今天没平仓"——行情偶发丢字段致 KeyError 不能被静默吞掉）。
_logger = logging.getLogger(__name__)


# ============================================================================
# 离场判定数据模型（ExitAction / ExitReason / ExitDecision）
# ============================================================================
class ExitAction(Enum):
    """离场动作（HOLD 持有 / CLOSE 平仓）。"""

    HOLD = "hold"
    CLOSE = "close"


class ExitReason(Enum):
    """离场原因（NONE 默认 / STOP_LOSS 止损 / TAKE_PROFIT 止盈 / TIMEOUT 时间止损）。"""

    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TIMEOUT = "timeout"
    NONE = "none"


@dataclass
class ExitDecision:
    """check_exit 的返回值（不可变值对象，线程安全）。

    字段物理意图：
        action:  HOLD（继续持有）/ CLOSE（触发离场）；
        reason:  离场原因（仅 CLOSE 时有意义，HOLD 时为 NONE）；
        new_stop: 移动止盈更新后的新止损价（None=不更新；有值=执行器应 update_plan
                  把持久化止损更新为此值，止损只上移锁定本金/利润）。
    """

    action: ExitAction
    reason: ExitReason = ExitReason.NONE
    new_stop: Optional[float] = None   # 移动止盈更新后的新止损（None=不更新）


# ============================================================================
# 离场纯函数 check_exit（回放验证器 + 实盘共用，杜绝双源真理）
# ============================================================================
def check_exit(pos: dict, bar: dict, bars_held: int, cfg) -> ExitDecision:
    """离场纯函数：止损/止盈/时间止损 + 移动止盈（盈亏平衡）。

    物理意图（蔡森原著离场优先级）：
        优先级链：止损 > 止盈 > 时间止损。止损是硬风控（防日内闪崩穿止损后反弹的
        假象），必须最先判定；止盈是目标达成；时间止损是资金占用机会成本兜底。

    参数：
        pos:       持仓 dict，必含 entry/stop/take_profit（+ 可选 take_profit_2x/
                   entry_bar/bars_held，本函数不依赖后三者）。
                   - entry:        成交价（盈亏平衡基准）；
                   - stop:         当前止损价（移动止盈激活后可能被上移）；
                   - take_profit:  第一波满足价（止盈目标）。
        bar:       当根 K 线 dict，必含 high/low/close。
        bars_held: 持仓交易日数（用于移动止盈激活 + 时间止损判定）。
        cfg:       StrategyConfig，用 trailing_to_breakeven / trailing_activation_bars /
                   max_holding_bars / timeout_exit_threshold 四个字段。

    返回：
        ExitDecision。HOLD 时 new_stop 可能有值（移动止盈已激活需更新持久化止损）；
        CLOSE 时 new_stop 同样可能有值（即便离场也回填新止损便于审计）。

    移动止盈（盈亏平衡锁定）：
        持仓 ≥ cfg.trailing_activation_bars 且 cfg.trailing_to_breakeven=True 且
        当前 stop < entry 时，把判定用的 stop 临时上移到 entry（盈亏平衡），
        并把 new_stop=entry 返回给执行器持久化（止损只上移不下移，锁定本金）。
        物理意图：持仓已激活移动止盈阶段，浮亏不应再扩大到原始 C 波低点止损幅度。
    """
    stop = pos["stop"]
    entry = pos["entry"]

    # —— 移动止盈：激活后将判定用的 stop 上移至盈亏平衡（entry）——
    # new_stop 记录需持久化的新止损（None=不更新）。仅在 stop < entry 时上移
    # （stop 已 ≥ entry 说明此前已上移过，不重复更新，止损只上移不下移）。
    new_stop: Optional[float] = None
    if (
        cfg.trailing_to_breakeven
        and bars_held >= cfg.trailing_activation_bars
        and stop < entry
    ):
        stop = entry
        new_stop = entry

    # —— 优先级 1：止损（日内最低价触及/跌破止损 → 立即平，记亏）——
    # 物理意图：硬风控，优先于止盈（防日内闪崩穿止损后反弹的假象）。
    if bar["low"] <= stop:
        return ExitDecision(ExitAction.CLOSE, ExitReason.STOP_LOSS, new_stop)

    # —— 优先级 2：第二波满足 take_profit_2x（与回测 backtest_replay 对齐，#16 修复）——
    # 物理意图：回测 _simulate_one_trade 离场优先级为 stop_loss > take_profit_2x >
    # take_profit，本函数原仅看第一波 take_profit，构成「回测一套/实盘一套」双源真理——
    # 回测在触及 2x 时按 2x 价记大盈 rr、实盘却可能在第一波即市价平仓，系统性使回测
    # avg_rr 虚高于实盘，可能放行实盘亏损策略通过上线 gate。现与回测同口径：先判 2x
    # （更优离场档）。pos 缺 take_profit_2x（None/缺失）时降级跳过本档，向后兼容。
    tp2x = pos.get("take_profit_2x")
    if tp2x is not None and bar["high"] >= tp2x:
        return ExitDecision(ExitAction.CLOSE, ExitReason.TAKE_PROFIT, new_stop)

    # —— 优先级 3：止盈（日内最高价触及/突破第一波满足 → 平，记盈）——
    # 物理意图：第一波满足点（颈线 + 1×H）达成，平仓锁盈（简化单笔全平）。
    if bar["high"] >= pos["take_profit"]:
        return ExitDecision(ExitAction.CLOSE, ExitReason.TAKE_PROFIT, new_stop)

    # —— 优先级 4：时间止损（持仓达 max_holding_bars 且浮盈 < threshold → 平）——
    # 物理意图：超时未达目标 + 浮盈不足阈值 = 资金占用机会成本过高，离场释放资金。
    # 浮盈比 = (close - entry) / entry（相对成交价的涨幅比例）。
    if bars_held >= cfg.max_holding_bars:
        profit = (bar["close"] - entry) / entry
        if profit < cfg.timeout_exit_threshold:
            return ExitDecision(ExitAction.CLOSE, ExitReason.TIMEOUT, new_stop)

    # —— 均未触发 → 继续持有（new_stop 可能需执行器持久化移动止盈后的新止损）——
    return ExitDecision(ExitAction.HOLD, new_stop=new_stop)


# ============================================================================
# ExecutionEngine：盘中轮询编排（依赖 trading_service 注入，测试可 mock）
# ============================================================================
class ExecutionEngine:
    """盘中 beat 编排引擎：tick_pullback（ARMED→FILLED）+ tick_exit（FILLED→CLOSED）。

    依赖注入（便于测试 mock）：
        trading_service: 交易服务对象，需提供：
            - get_status() -> dict（同步，返 {connected, locked, mode}）；
            - submit_order(order, *, dry_run, confirm) -> dict（async，过 10 关风控 + EMT）。
        cfg: StrategyConfig（check_exit 用离场参数 + check_pullback 用回踩区间）。

    设计原则（CLAUDE.md 极简）：
        - 不在本层另造下单逻辑/风控规则——submit_order 复用 trading_service 完整链路；
        - check_exit / check_pullback 是纯函数/纯方法（无 I/O），可独立单测；
        - tick_* 是 async 编排方法（I/O 边界），用 try/except 隔离单计划异常。
    """

    def __init__(self, trading_service, cfg):
        """注入 trading_service 与 cfg，绑定 storage 模块（便于 monkeypatch 测试）。"""
        self.trading = trading_service
        self.cfg = cfg
        # storage 在模块顶导入（from caisen import storage），测试用 monkeypatch
        # 替换 caisen.execution.storage.load_plans/update_plan 隔离文件 I/O。

    def check_pullback(self, plan: dict, quote: dict) -> bool:
        """ARMED→FILLED 触发判定：盘中触及回踩挂单区间 [entry_lower, entry_upper]。

        物理意图：回踩挂单挂在 [entry_lower, entry_upper] 区间，当根 K 线的 low/high
        触及该区间（low ≤ entry_upper 且 high ≥ entry_lower）即视为挂单应被触发成交。

        参数：
            plan:  ARMED 计划 dict，必含 entry_upper / entry_lower。
            quote: 当根行情 dict，含 high / low（None 时返回 False，防御性）。

        返回：
            True  : 触及回踩区间（应 submit_order 限价挂 entry_upper 买入）；
            False : 未触及 / quote 为 None。
        """
        if quote is None:
            return False
        # low 默认 +∞（价未跌到挂单上限之下）、high 默认 -∞（价未升到挂单下限之上），
        # 缺字段时保守判定为不触发（防脏数据误下单）。
        low = quote.get("low", float("inf"))
        high = quote.get("high", float("-inf"))
        return low <= plan["entry_upper"] and high >= plan["entry_lower"]

    async def _get_quote(self, symbol: str) -> Optional[dict]:
        """查标的实时行情（tick 内部用，子类/生产可覆写接入真实行情源）。

        物理意图：盘中 beat 需要当根 K 线的 high/low/close 判定回踩触发 / 离场条件。
        默认实现返回 None（占位，生产由 Task 3/4 service 注入真实行情查询）；
        测试通过 monkeypatch 替换为 AsyncMock 返回固定行情。

        参数：
            symbol: 标的代码。

        返回：
            {high, low, close} dict；无行情时返回 None（check_pullback/check_exit 容错）。
        """
        # 默认占位（生产由 service 注入真实行情查询；测试 monkeypatch 覆写）。
        # 不在此处耦合 trading_service（行情查询不在 trading_service 必选契约内），
        # 由 Task 3/4 的 service 层统一接入 DataLakeReader / 实时行情源。
        return None

    def _today_bar(self) -> int:
        """获取今日 K 线序号（entry_bar 记录用，便于 bars_held 推算）。

        物理意图：FILLED 时记录 entry_bar（成交当日 K 线序号），后续 tick_exit
        推算 bars_held = today_bar - entry_bar（持仓交易日数）。

        默认实现返回 0（占位，生产由 service 注入交易日历序号）；
        测试通过对象属性直接覆写或扩展。本字段为审计辅助，离场判定核心是
        check_exit 的 bars_held 参数（由调用方推算后传入）。
        """
        return 0

    async def tick_pullback(self) -> None:
        """beat 调用：遍历 ARMED 计划，触及回踩区间 → submit_order 限价挂 entry_upper。

        编排流程（断线不补发）：
            1. trading.get_status() 判定连接态：locked 或 not connected → return（跳过本轮）；
            2. storage.load_plans(status="ARMED") 拉所有待回踩计划；
            3. 逐计划：_get_quote(symbol) → check_pullback(plan, quote)；
            4. 触及回踩 → submit_order(OrderRequest(buy, price=entry_upper))；
            5. 成交 → update_plan(status="FILLED", entry_bar=today_bar)。

        单计划异常隔离：try/except 包裹单个计划处理（不中断本轮其它计划）。
        """
        # —— 断线不补发：locked 或 not connected → 跳过本轮 ——
        # 物理意图：断线瞬间行情/下单均不可靠，本轮跳过，等下一轮重连后再处理。
        status = self.trading.get_status()
        if status.get("locked") or not status.get("connected"):
            return

        # —— 遍历 ARMED 计划（storage.load_plans 跨日期合并）——
        for plan in storage.load_plans(status="ARMED"):
            try:
                quote = await self._get_quote(plan["symbol"])
                if not self.check_pullback(plan, quote):
                    continue   # 未触及回踩区间，跳过
                # —— 触及回踩 → 限价挂 entry_upper 买入 ——
                # 复用 trading_service.submit_order（过 check_order 10 关风控 + EMT 网关）。
                # 延迟 import 避免模块加载循环（trading.execution_gateway 依赖面较广）。
                from trading.execution_gateway import OrderRequest

                order = OrderRequest(
                    symbol=plan["symbol"],
                    qty=plan["shares"],
                    side="buy",
                    price=plan["entry_upper"],
                )
                result = await self.trading.submit_order(order, dry_run=False, confirm=True)
                # 【B-4 修复】仅在真实成交才推进 FILLED，杜绝幽灵持仓。
                # EMT submit_order 返回 state=SUBMITTED（限价单已提交、成交靠异步回报），
                # 若无视 state 直接标 FILLED，会在未成交单上建出「幽灵持仓」，tick_exit
                # 随后可能在其上发市价 SELL → 对不存在的持仓发卖单（裸卖空/拒单/敞口失控）。
                state = (result or {}).get("state")
                if state in ("FILLED", "PARTIAL_FILLED"):
                    # —— 真实成交 → 状态推进 FILLED（记录 entry_bar 便于后续 bars_held 推算）——
                    storage.update_plan(
                        plan["plan_id"],
                        status="FILLED",
                        entry_bar=self._today_bar(),
                    )
                elif state in ("REJECTED", "FAILED"):
                    # —— 废单/失败 → 回退 PENDING_APPROVAL 待人工，绝不标 FILLED ——
                    # Why 回退而非留 ARMED：废单不会自愈成成交，留 ARMED 会每轮重复发废单；
                    # 回退审核让人工介入（资金不足/参数非法等原因需人工裁决）。
                    _logger.warning(
                        "tick_pullback 计划 %s(%s) 下单被拒/失败 state=%s msg=%s，回退审核",
                        plan.get("plan_id"), plan.get("symbol"),
                        state, (result or {}).get("message"),
                    )
                    storage.update_plan(
                        plan["plan_id"], status="PENDING_APPROVAL",
                        note=f"order_{state}",
                    )
                # SUBMITTED / 其它中间态：限价单排队未成交，保持 ARMED，等成交回报推进
                # （完整闭环需 P1-9 网关对账/回调把 SUBMITTED→FILLED 推进，本处先堵乐观标记）。
            except Exception as e:
                # 单计划异常不中断本轮其它计划（边界隔离）。
                # 不在此处重试（断线/限频由 trading_service 内部熔断兜底；
                # 状态机一致性由下一轮 beat 重新读 storage 自然收敛）。
                # 实盘可观测性（CLAUDE.md 显式至上）：行情源偶发丢字段（quote 缺
                # high/low/close）会抛 KeyError，若静默吞掉则运维无法定位"为什么
                # 今天没成交"。此处落 warning 日志记录 plan_id + symbol + 异常类型 +
                # 详情，便于回溯。
                _logger.warning(
                    "tick_pullback 计划 %s(%s) 处理异常，本轮跳过：%s: %s",
                    plan.get("plan_id"), plan.get("symbol"),
                    type(e).__name__, e,
                )
                continue

    async def tick_exit(self) -> None:
        """beat 调用：遍历 FILLED 持仓，check_exit 命中 CLOSE → 市价平仓。

        编排流程（持仓风控持续运行，B-8）：
            1. trading.get_status() 判定连接态：仅 not connected → return（断线无可靠行情）；
               locked（风险否决）不再跳过——已有持仓的止损/止盈是风险缩减动作，必须持续；
            2. storage.load_plans(status="FILLED") 拉所有持仓中计划；
            3. 逐计划：_get_quote → check_exit(pos, bar, bars_held, cfg)；
            4. CLOSE → submit_order(sell 市价)；仅 state∈{FILLED,PARTIAL_FILLED} 才 update_plan(CLOSED)，
               拒单/失败保持 FILLED 等下一轮重试（防幽灵了结，B-4 对称）；
            5. HOLD + new_stop != None → update_plan(stop=new_stop)（移动止盈止损上移）。

        bars_held 推算：plan["bars_held"]（由 service 层按交易日历推算后写入 plan dict），
        缺省时按 entry_bar 推算 today_bar - entry_bar（_today_bar 占位为 0，生产注入）。
        """
        # —— 闸门：仅断线（not connected）跳过；locked（风险否决）不跳过（B-8 离场持续）——
        # Why 不再因 locked 跳过：风险否决锁态只应停新开仓(pullback)，离场是风险缩减须持续；
        # 断线时无可靠行情/下单通道，才保守跳过（_get_quote 返 None 亦会逐持仓 continue 兜底）。
        status = self.trading.get_status()
        if not status.get("connected"):
            return

        # —— 遍历 FILLED 持仓 ——
        for plan in storage.load_plans(status="FILLED"):
            try:
                quote = await self._get_quote(plan["symbol"])
                if quote is None:
                    continue   # 无行情，跳过（无法判定离场条件）
                # 组装 check_exit 入参 pos / bar
                pos = {
                    "entry": plan["entry"],
                    "stop": plan["stop"],
                    "take_profit": plan["take_profit"],
                    "take_profit_2x": plan.get("take_profit_2x"),
                }
                bar = {"high": quote["high"], "low": quote["low"], "close": quote["close"]}
                # bars_held：优先读 plan 内字段（service 层注入），缺省按 entry_bar 推算
                bars_held = plan.get("bars_held")
                if bars_held is None:
                    entry_bar = plan.get("entry_bar", 0)
                    bars_held = max(self._today_bar() - entry_bar, 0)

                decision = check_exit(pos, bar, bars_held, self.cfg)

                if decision.action == ExitAction.CLOSE:
                    # —— 命中离场 → 市价平仓（price=None 即市价）——
                    from trading.execution_gateway import OrderRequest

                    order = OrderRequest(
                        symbol=plan["symbol"],
                        qty=plan["shares"],
                        side="sell",
                        price=None,   # 市价平仓（A 股市价单走对手价/最优五档）
                    )
                    result = await self.trading.submit_order(order, dry_run=False, confirm=True)
                    # 【B-4 对称】仅真实成交才标 CLOSED，防幽灵了结：卖单被拒/未成交时
                    # 若盲目标 CLOSED，会把仍持有的仓位移出监控 → 敞口失控且与券商不一致。
                    # 保持 FILLED，下一轮 tick_exit 重新评估（止损只更急）。
                    state = (result or {}).get("state")
                    if state in ("FILLED", "PARTIAL_FILLED"):
                        storage.update_plan(plan["plan_id"], status="CLOSED")
                    else:
                        _logger.warning(
                            "tick_exit 持仓 %s(%s) 平仓未成交 state=%s msg=%s，保持 FILLED 待下轮重试",
                            plan.get("plan_id"), plan.get("symbol"),
                            state, (result or {}).get("message"),
                        )
                elif decision.new_stop is not None:
                    # —— HOLD + 移动止盈激活 → 持久化新止损（止损只上移）——
                    storage.update_plan(plan["plan_id"], stop=decision.new_stop)
            except Exception as e:
                # 单持仓异常不中断本轮其它持仓（边界隔离）。
                # 实盘可观测性（CLAUDE.md 显式至上）：行情源偶发丢字段（quote 缺
                # high/low/close）会抛 KeyError，若静默吞掉则该持仓本轮离场判定
                # 整轮失效，运维无法定位"为什么今天没平仓"。此处落 warning 日志
                # 记录 plan_id + symbol + 异常类型 + 详情，便于回溯。
                _logger.warning(
                    "tick_exit 持仓 %s(%s) 处理异常，本轮跳过：%s: %s",
                    plan.get("plan_id"), plan.get("symbol"),
                    type(e).__name__, e,
                )
                continue
