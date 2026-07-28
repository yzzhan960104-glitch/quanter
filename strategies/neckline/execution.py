# -*- coding: utf-8 -*-
"""颈线法【执行层】离场判定纯函数（Task 4 · U3 执行单源基石）。

物理定位（spec §2 目标2 + §4.2 + D5/D6）：
    decide_exit 是 simulate_exit（strategies/neckline/backtest.py:125-199 持有期循环）
    的「单根 K 线判定」纯函数化——把 simulate_exit 里每根 bar 的「该不该离场/为何离场」
    分支判定原样搬到 decide_exit(state, bar, cfg) → NecklineExitDecision。

    本 task 完成后 decide_exit 是【已测但未挂接】的纯函数——
      - simulate_exit 逐根循环改调 decide_exit：Task 5（golden 冠军零退化铁证）
      - 实盘 stop_loss_monitor 改调 decide_exit：Task 9（dry_run+模拟盘双验）

    strangler 红线：decide_exit 逻辑【零改动】于 simulate_exit:125-199（只搬成纯函数，
    优先级/阈值/trailing 全照搬）。Task 5 golden 守护、Task 9 实盘守护都依赖此等价性。

── NecklineExitDecision vs ExitDecision ──
    NecklineExitDecision：本模块新建（颈线法专属，含 portion 分级字段，simulate_exit 用）。
    ExitDecision（trading/compute/exit.py:71）：caisen 遗产，check_exit 用（Task 6 删）。
    两者字段不同——ExitDecision 无 portion（caisen 单笔全平），NecklineExitDecision 有
    portion（颈线法分级 lot1/lot2）。故不共用 dataclass。

── ExitAction/ExitReason 迁移（Controller #8）──
    ExitAction(Enum)/ExitReason(Enum) 原 in trading/compute/exit.py:44-57（caisen 遗产），
    颈线法 decide_exit 也要用（动作/原因枚举跨形态共用，纯枚举无 caisen 业务逻辑）。
    迁移策略：class 定义【物理剪到】本模块（execution.py），exit.py 改
    `from strategies.neckline.execution import ExitAction, ExitReason`（re-export 垫片），
    保 check_exit:79 + ExitDecision:71 不破（Task 6 删 check_exit/ExitDecision 时一并清垫片）。
    trading/compute/__init__.py 的 re-export 通过垫片透传，不破。

    ExitReason 本 task 新增 CANCEL_ON 成员（颈线法 pending 撤单专属，caisen 无此场景）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

# trailing stop 纯函数（海龟 trailing 离散，从 trading/compute/stop.py 迁入同包）。
# 迁移原因（Task 4 · U3 执行单源 + layer contract 铁律2）：
#   compute_stop_price 原在 trading/compute/stop.py（从 scripts/neckline_backtest.simulate_exit
#   迁出），但它本质是颈线法专属的海龟 trailing 离散——真正归属是 strategies/neckline/。
#   本 task decide_exit（颈线法执行单源）需要调它，但 strategies/ 铁律零 trading 依赖
#   （test_strategies_no_trade_dependency），且 Controller #8 迁 ExitAction/ExitReason 后
#   execution.py → trading.compute.stop 会构成循环 import。故把 compute_stop_price 物理
#   迁回颈线法执行层（execution.py，它的算法本体家乡），trading/compute/stop.py 改垫片
#   re-export 保 trading/engine.py 等调用点零改动（Task 5/6 清垫片时一并收）。
#
# strangler 红线：函数体【零改动】（7 行纯数学，原样搬位置），docstring 保留同源说明。
def compute_stop_price(
    neckline: float,
    atr: float,
    holding_days: int,
    stop_atr_mult: float,
    grace: int,
    step: float,
    floor: float | None,
) -> float:
    """给定持有天数算当日止损价（颈线基准，trailing 离散）。

    物理意图（与 simulate_exit:160-173 完全同源）：
    - grace 天内：用 base_stop（颈线 - stop_atr_mult×ATR，固定，给趋势确认空间）；
    - grace 天后：每日收紧 step×ATR（eff_mult 递减），到 floor 卡底（收紧上限）；
    - grace=0/step=0：退化为固定止损（=base_stop，兼容旧行为）。

    离散化（二期）：盘后对每只持仓调本函数重算【次日】固定止损价；盘中监控用此固定价，
    不移动（符合 spec「盘中不调整」）。回测里是逐根 K 线调；实盘改为每日一次。
    """
    base_stop = neckline - stop_atr_mult * atr
    if grace and step and holding_days > grace:
        eff_mult = stop_atr_mult - (holding_days - grace) * step
        if floor is not None:
            eff_mult = max(eff_mult, floor)
        return neckline - eff_mult * atr
    return base_stop


# ============================================================================
# 离场判定数据模型（ExitAction / ExitReason · 从 trading/compute/exit.py 迁入）
# ============================================================================
class ExitAction(Enum):
    """离场动作（HOLD 持有 / CLOSE 平仓 / CANCEL 撤单）。

    物理意图：
        HOLD：继续持有/挂单（未触发任何离场/撤单条件）；
        CLOSE：触发离场（止损/止盈/超时，含部分平仓 portion<1.0）；
        CANCEL：pending 挂单撤单（涨幅兑现，回踩是退潮，撤单不买）。

    迁移来源（Controller #8）：原 trading/compute/exit.py:44-47（caisen 遗产只有
    HOLD/CLOSE，本 task 新增 CANCEL 服务颈线法 pending 撤单场景）。
    """

    HOLD = "hold"
    CLOSE = "close"
    CANCEL = "cancel"


class ExitReason(Enum):
    """离场原因（NONE 默认 / STOP_LOSS 止损 / TAKE_PROFIT 止盈 / TIMEOUT 超时 / CANCEL_ON 撤单）。

    迁移来源（Controller #8）：原 trading/compute/exit.py:51-57（caisen 遗产）。
    本 task 新增 CANCEL_ON（颈线法 pending 期 high≥cancel_on 撤单专属，caisen 无此场景）。
    """

    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TIMEOUT = "timeout"
    CANCEL_ON = "cancel_on"   # pending 期撤单（颈线法专属，2026-07-29 Task 4 新增）
    NONE = "none"


@dataclass(frozen=True)
class NecklineExitDecision:
    """decide_exit 的返回值（不可变值对象，线程安全 · Controller #7）。

    字段物理意图：
        action:   HOLD（继续）/ CLOSE（平仓，含部分平仓 portion<1.0）/ CANCEL（撤单）；
        reason:   离场原因（仅 CLOSE/CANCEL 时有意义，HOLD 时为 NONE）；
        portion:  平仓比例 0.0~1.0（STOP_LOSS/TP2/TIMEOUT=1.0 全平；TP1=tp1_portion 卖 lot1；
                  HOLD=0.0 不平；CANCEL=1.0 全撤）。调用方据 portion 判定「全平 vs 部分平」。
        new_stop: 当根 trailing 算出的止损价（compute_stop_price 结果，供调用方观测/推进
                  持久化止损；holding 阶段始终有值，pending 阶段 None）。
    """

    action: ExitAction
    reason: ExitReason = ExitReason.NONE
    portion: float = 0.0
    new_stop: Optional[float] = None


# ============================================================================
# 离场纯函数 decide_exit（simulate_exit:125-199 单根判定 · strangler 等价红线）
# ============================================================================
def decide_exit(state: dict, bar: dict, cfg: dict) -> NecklineExitDecision:
    """颈线法【单根 K 线】离场判定纯函数（U3 执行单源基石）。

    物理定位（spec §4.2 D5/D6）：
        把 simulate_exit（backtest.py:125-199）里每根 bar 的分支判定原样抽成纯函数。
        调用方（Task 5 simulate_exit 逐根循环 / Task 9 实盘 stop_loss_monitor）负责：
          - 迭代 bar 序列、维护 state（holding_days 推进、lot1/lot2_open 翻转、is_last 判定）；
          - pending 期成交判定（low≤buy_limit → buy_idx，decide_exit 不判成交，Controller #3）；
          - 据返回的 action/reason/portion 决定「全平 break / 部分平 continue / 撤单 break / 继续」。

    参数（Controller #1：state 放运行时变量，cfg 放静态参数）：
        state: 运行时状态 dict，必含字段：
            phase:        "pending"（挂单等待期）/ "holding"（成交后持有期）；
            entry:        成交价（holding 必填，盈亏基准；pending 不用）；
            tp1:          第一止盈价（颈线+tp1_h_mult×H，holding 必填）；
            tp2:          第二止盈价（颈线+tp_h_mult×H，holding 必填）；
            cancel_on:    撤单阈值（pending 必填，None=不撤单放飞；holding 不用）；
            neckline:     颈线价（holding 必填，trailing 基准）；
            atr:          ATR 值（holding 必填，trailing/stop 计算用）；
            holding_days: 持有交易日数（holding 必填，i-buy_idx；trailing grace/step 用）；
            is_last:      是否持有期最后一根（holding 必填，超时判定用）；
            lot1_open:    lot1 是否仍持有（holding 必填，True=未卖 lot1；默认 True 向后兼容）；
            lot2_open:    lot2 是否仍持有（holding 必填，True=未卖 lot2；默认 True 向后兼容）。
        bar:  当根 K 线 dict，必含 high/low/close（pending 只用 high，holding 用 high/low）。
        cfg:  静态参数 dict（整个持有期不变），必含键：
            stop_atr_mult:    识别层止损 ATR 倍数（← id_cfg，simulate_exit:117/167）；
            trailing_grace:   宽限天数 b（← exec，simulate_exit:164，default 0）；
            trailing_step:    收紧速度 a（← exec，simulate_exit:165，default 0.0）；
            trailing_floor:   收紧上限（← exec，simulate_exit:168，default None）；
            tp1_portion:      lot1 占比（← exec，simulate_exit:215，default 0.5）；
            max_holding:      超时持仓日（← exec，cfg 冗余备用，is_last 由调用方算）。

    返回：NecklineExitDecision（action/reason/portion/new_stop）。

    ── strangler 等价分支表（与 simulate_exit:125-199 逐行对应）──
        pending 期（simulate_exit:128-145 循环体）：
            high≥cancel_on → CANCEL/CANCEL_ON/1.0/None（= simulate_exit:130 skip_target_met）
            else           → HOLD/NONE/0.0/None（pending 不判 trailing，new_stop=None）
            注：成交（low≤buy_limit）由调用方判 buy_idx，不是 decide_exit 的决策。

        holding 期（simulate_exit:156-199 循环体，优先级链严格照搬）：
            priority 1 (L174): low ≤ trailing_stop → CLOSE/STOP_LOSS/1.0/stop（止损全平）
            priority 2 (L180): lot2_open and high ≥ tp2 → CLOSE/TAKE_PROFIT/1.0/stop
                               （tp2 全平 lot1+lot2；simulate_exit:183 lot1 同日一并卖）
            priority 3 (L189): lot1_open and high ≥ tp1 → CLOSE/TAKE_PROFIT/tp1_portion/stop
                               （只卖 lot1，simulate_exit:192 continue 持 lot2；调用方见
                               CLOSE+portion<1.0 应 continue 循环，勿 break）
            priority 4 (L193): is_last → CLOSE/TIMEOUT/1.0/stop（超时强制平，不判浮盈）
            else           → HOLD/NONE/0.0/stop（继续持有，new_stop 供观测/推进）

    trailing stop 参数映射（Controller #6 红线，与 simulate_exit:160-173 内联同源）：
        stop = compute_stop_price(
            neckline=state["neckline"],
            atr=state["atr"],
            holding_days=state["holding_days"],
            stop_atr_mult=cfg["stop_atr_mult"],
            grace=cfg.get("trailing_grace", 0),
            step=cfg.get("trailing_step", 0.0),
            floor=cfg.get("trailing_floor"),
        )
        compute_stop_price docstring 明示「与 simulate_exit 完全同源」——参数字面对齐
        simulate_exit:164-173（grace=exec.get, step=exec.get, floor=exec.get）。
    """
    phase = state["phase"]

    # ── pending 期（挂单等待回踩，simulate_exit:128-145）──
    if phase == "pending":
        cancel_on = state.get("cancel_on")
        # strangler 等价 simulate_exit:130：
        #   if cancel_on is not None and float(sym_df["high"].iloc[i]) >= cancel_on:
        #       return {exit_reason: "skip_target_met", ...}
        # Controller #3：pending 用 high（盘中摸高）不是 close——执行层 pending 是盘中，
        # 有 high；识别层 cancel_on 才用 close（detect_signal 已实现，本 task 不涉及）。
        if cancel_on is not None and float(bar["high"]) >= cancel_on:
            return NecklineExitDecision(
                action=ExitAction.CANCEL,
                reason=ExitReason.CANCEL_ON,
                portion=1.0,
                new_stop=None,
            )
        # pending 期未撤单 → 继续等回踩（成交由调用方判 low≤buy_limit，不是本函数职责）
        return NecklineExitDecision(
            action=ExitAction.HOLD,
            reason=ExitReason.NONE,
            portion=0.0,
            new_stop=None,
        )

    # ── holding 期（成交后持有，simulate_exit:156-199）──
    # 先算当根 trailing stop（与 simulate_exit:160-173 内联同源 = compute_stop_price）
    # 参数映射红线（Controller #6）：stop_atr_mult ← cfg（simulate_exit 用 id_cfg），
    # grace/step/floor ← cfg（simulate_exit 用 exec.get）——本 task 统一从 cfg 取。
    # 调本模块（execution.py）内的 compute_stop_price，无跨层依赖。
    stop = compute_stop_price(
        neckline=state["neckline"],
        atr=state["atr"],
        holding_days=state["holding_days"],
        stop_atr_mult=cfg["stop_atr_mult"],
        grace=cfg.get("trailing_grace", 0) or 0,
        step=cfg.get("trailing_step", 0.0) or 0.0,
        floor=cfg.get("trailing_floor"),
    )

    high = float(bar["high"])
    low = float(bar["low"])

    # lot1/lot2 状态默认 True（向后兼容老调用未传；simulate_exit:152 初始化 True）
    lot1_open = state.get("lot1_open", True)
    lot2_open = state.get("lot2_open", True)

    # ── priority 1（simulate_exit:174）：止损（动态 trailing）──
    # 物理意图：硬风控，优先于止盈（防日内闪崩穿止损后反弹的假象）。lot1+lot2 全平。
    # strangler 等价 simulate_exit:174-179：
    #   if low <= stop: → lot1_pnl=lot2_pnl=(stop-entry)/entry; break (exit_reason=stop_loss)
    if low <= stop:
        return NecklineExitDecision(
            action=ExitAction.CLOSE,
            reason=ExitReason.STOP_LOSS,
            portion=1.0,
            new_stop=stop,
        )

    # ── priority 2（simulate_exit:180）：tp2（lot1 同日一并卖，全平）──
    # 物理意图：触及第二止盈（颈线+2H），lot1+lot2 同日全部卖出锁大盈。
    # strangler 等价 simulate_exit:180-188：
    #   if lot2_open and high >= tp2: → lot2 卖 + lot1 同日一并卖; break (exit_reason=tp2)
    # portion=1.0 = 全平（lot1+lot2 100%）。
    tp2 = state["tp2"]
    if lot2_open and high >= tp2:
        return NecklineExitDecision(
            action=ExitAction.CLOSE,
            reason=ExitReason.TAKE_PROFIT,
            portion=1.0,
            new_stop=stop,
        )

    # ── priority 3（simulate_exit:189）：tp1（只卖 lot1，lot2 继续持）──
    # 物理意图：触及第一止盈（颈线+1H），只卖 lot1 减仓，lot2 续持博 tp2。
    # strangler 等价 simulate_exit:189-192：
    #   if lot1_open and high >= tp1: → lot1 卖; continue (exit_reason 不变, lot1_open=False)
    # 注意 simulate_exit 是 continue 不是 break——调用方见 CLOSE+portion<1.0 应继续循环 lot2。
    # portion=tp1_portion（lot1 占比，0.5=卖一半仓位）。
    tp1 = state["tp1"]
    if lot1_open and high >= tp1:
        return NecklineExitDecision(
            action=ExitAction.CLOSE,
            reason=ExitReason.TAKE_PROFIT,
            portion=float(cfg["tp1_portion"]),
            new_stop=stop,
        )

    # ── priority 4（simulate_exit:193）：超时（is_last，不判浮盈 threshold）──
    # 物理意图：持有期最后一根，无论浮盈多少都强制收盘平（资金占用机会成本兜底）。
    # strangler 等价 simulate_exit:193-199：
    #   if is_last: → lot1/lot2 各用 close 算 pnl 强制平 (exit_reason=timeout)
    # Controller #5：is_last 直接平，【不判浮盈 threshold】（brief 描述不精确，以源为准）。
    if state.get("is_last", False):
        return NecklineExitDecision(
            action=ExitAction.CLOSE,
            reason=ExitReason.TIMEOUT,
            portion=1.0,
            new_stop=stop,
        )

    # ── 均未触发 → 继续持有（new_stop 供调用方观测/推进持久化止损）──
    return NecklineExitDecision(
        action=ExitAction.HOLD,
        reason=ExitReason.NONE,
        portion=0.0,
        new_stop=stop,
    )
