# -*- coding: utf-8 -*-
"""离场纯函数 check_exit（trading/compute/exit.py · functional core）。

物理定位（Layer2 阶段2 · spec §3.5/§4）：
    本模块属 trading/compute/ 子包——纯决策函数（无 I/O、无状态、确定性），回测与
    实盘共用同一离场判定函数（杀手不变量）。check_exit 输入 (pos, bar, bars_held, cfg)
    输出 ExitDecision，仅依赖标准库 dataclasses/enum/typing，零外部依赖。

    ── 单源真理契约（核心红线：杜绝双源真理）──
    历史：实盘 ExecutionEngine 用 check_exit，回放验证器 backtest_replay 内联独立
    离场逻辑——构成「回测一套离场规则 / 实盘另一套」的双源真理隐患（回测调优数据
    可能不反映实盘行为）。统一抽到本纯函数模块后，回测与实盘共用同一判定函数。

    用户决策（已确认）：回测对齐实盘、引入移动止盈（trailing_to_breakeven 默认 True）。
    即单源化后 backtest_replay 经 check_exit 离场，回测会新增移动止盈行为（持仓
    ≥trailing_activation_bars 且浮亏时止损上移至 entry）。用户接受回测结果变化
    （旧调优数据失效，换回测真实反映实盘）。

    ── 离场优先级（蔡森原著 + 防日内闪崩）──
    优先级链：止损 > 止盈 > 时间止损。
      - 止损最先判定：硬风控（防日内闪崩穿止损后反弹的假象）；
      - 第二波 take_profit_2x 优先于第一波 take_profit：触及 2x 按更优离场档判定
        （回测同口径按 2x 价记大盈 rr）；pos 缺 take_profit_2x 时降级只看第一波；
      - 时间止损兜底：超时未达目标 + 浮盈不足阈值 → 砍亏释放资金。

迁移纪律（strangler 红线）：
    check_exit 逻辑【零改动】（含移动止盈 trailing 逻辑），只搬位置。
    Layer2 阶段2：从 execution/exit_logic.py 物理迁入 trading/compute/exit.py
    （git mv 保 is 同源）。Layer2 阶段4：execution 包整体解散，exit_logic 垫片 +
    execution/__init__ re-export 一并删除——check_exit 现单源于 trading.compute.exit，
    经 trading.compute 包 re-export 暴露。回测侧 backtest.replay 经本函数离场
    （杀手不变量：回测与未来实盘 reducer 共用同一判定，杜绝双源真理）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


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
        cfg:       鸭子类型配置对象（属性访问 ``cfg.trailing_to_breakeven`` 形态；
                   NecklineConfig dataclass 或其他具同名属性的对象均可，原 caisen
                   StrategyConfig 已随形态退役删除，此处不绑死类型），用
                   trailing_to_breakeven / trailing_activation_bars /
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
