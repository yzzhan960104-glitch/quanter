# -*- coding: utf-8 -*-
"""颈线法多标的回测：流动性 top30 标的 × 颈线法识别 × 挂单回踩持有期模拟。

用户交易规则（v2 · 2026-07-18）：
    进场 = 突破后在 颈线+1·ATR 挂买单，有效期 max_wait 天，回踩触发即按挂单价成交；
           不回踩则放弃（防追高，但可能错过直接飞的信号）。
    止损 = 颈线−1·ATR（破位 1ATR 确认，防假摔洗盘）。
    止盈 = 分级 50%@颈线+H，50%@颈线+2H。
    超时 = 成交后 max_holding 日未达任一止盈，收盘卖剩余。

盈亏结构：risk=entry−stop=2·ATR（固定对称），收益=H−ATR / 2H−ATR，
盈亏比由形态深度 H/ATR 决定。

用法：
    PYTHONIOENCODING=utf-8 python -u strategies/neckline/backtest.py
"""
from __future__ import annotations

import hashlib
import json
import math
import os   # main():511 os.makedirs("logs") 用（U5 Task 8 补——原顶部漏 import os 致潜伏 NameError）
# hashlib/json：R6-1（2026-08-26）识别事件缓存的稳定指纹（_stable_cfg_hash/_sym_data_hash）

import numpy as np
import pandas as pd

# 本模块原在 scripts/（平级 from neckline_method_v0），Layer2 Task 1.5 收口进 strategies/neckline/
# 子包后改相对 import（同包内 .method_v0）。
# P1（2026-08-13 · spec §2.1）：scan_symbol 识别循环改走 detect_signal_fast（全序列数组
# fast path），detect_signal 仍保留 import（test_param_iter_kernel_same_source 断言
# bk.detect_signal is method_v0.detect_signal 的同源 binding 契约不变）。
from .method_v0 import (DEFAULTS, TOPS_WINDOW, compute_atr, decay_weights_of,
                        detect_signal, detect_signal_fast, local_extrema_mask)

# Task 5 · U3 执行单源：持有期逐根离场判定改调 decide_exit（Task 4 建好的纯函数），
# 让回测 simulate_exit 与实盘 stop_loss_monitor（Task 9）共用 decide_exit 单源。
# decide_exit 内部调 compute_stop_price 算 trailing，本模块不再内联算 trailing（消除数学双份）。
from .execution import decide_exit, ExitAction, ExitReason

# CR-2（2026-08-15 · 审计 spec A5+C1）：入场价位三件套（挂单价/止损/tp1/tp2/撤单阈值）
# 改调同包 price_levels 单源——回测与实盘（trading/compute/plan.py）共用同一价位数学，
# 等价性由 tests/trading/test_price_levels_golden.py 用迁移前快照逐位钉死。
# 参数仍从 id_cfg（识别层）/exec（执行层）两口读（结构不动），缺参回落 PRICE_LEVEL_DEFAULTS。
from .price_levels import PRICE_LEVEL_DEFAULTS, compute_price_levels


# 持有期参数（用户规则，可调）—— 保留为向后兼容常量
MAX_HOLDING = 15           # 成交后超时持仓周期（交易日）
MAX_WAIT = 5               # 挂单等待回踩成交有效期（≈一周）
COOLDOWN = 5               # 信号去重冷却期（相邻信号合并为一次，同形态多日触发）
TOP_N = 30                 # 扫描流动性 top N 标的

# 执行层参数（param_iter 19维调参的执行层 7 旋钮；simulate_exit/scan_symbol 读此）
# 与识别层 DEFAULTS 分离：识别管"形态判定"，执行管"挂单/止盈/仓位/撤单"
EXEC_DEFAULTS = {
    "max_holding": 15,           # 成交后超时持仓日（=MAX_HOLDING）
    "max_wait": 5,               # 挂单等待回踩成交有效期（=MAX_WAIT）
    "cooldown": 5,               # 信号去重冷却（=COOLDOWN）
    "buy_limit_atr_mult": 1.0,   # 挂单价 = 颈线 + N×ATR（N=1.0=颈线上方1ATR挂买单等回踩）
    "tp1_h_mult": 1.0,           # 止盈1 = 颈线 + N×H（第一波减仓位，N=1.0=1H）
    "tp1_portion": 0.5,          # 止盈1 减仓比例（lot1 占比，剩余 lot2 持到 tp2；0.5=各半）
    "cancel_thresh_mult": 1.0,   # 等待期撤单阈值 = 颈线 + N×H（high≥此价即撤单防追高；
                                 # N=1.0=涨到tp1即撤(旧默认)；None=不撤单放飞所有信号）
    # 时间驱动移动止损（海龟风格，用户 2026-07-20）：前 grace 天宽限（止损=颈线-stop_mult×ATR
    # 固定不动，给趋势确认空间），grace 天后每日收紧 step×ATR（趋势不确认逐步退出），
    # 到 floor×ATR 卡住。grace=0/step=0 退化为固定止损（兼容旧行为）。详见 memory
    # neckline-trailing-stop.md。
    "trailing_grace": 0,         # 宽限天数 b（前 b 天不收紧；候选 5/10）
    "trailing_step": 0.0,        # 收紧速度 a（ATR/日；候选 0.05/0.1/0.15）
    "trailing_floor": 0.5,       # 最低 ATR 倍数（收紧上限；0=到颈线，0.5=颈线-0.5ATR）
    # 交易成本（plan Task 12 · P1-9 对齐实盘）：万三佣金 + 卖0.05%印花 + 0.001%过户。
    # simulate_exit 按费率扣 entry/exit pnl（lot1/lot2 各卖各扣 sell_rate=双倍佣）。
    # _cost(side,qty,price) 金额口径（含 min5）供实盘；回测用费率（百分比 pnl 无 notional）。
    "commission_rate": 0.0003,   # 佣金万三（双边，买+卖各一次）
    "stamp_rate": 0.0005,        # 印花税卖 0.05%（单边卖出）
    "transfer_rate": 0.00001,    # 过户费 0.001%（双边，沪市）
    # R6-5 腿 A/B 受控原型参数（2026-08-26 用户裁决「都测试一下」；默认关=零行为变化，
    # 由 tests/test_r6_phantom_and_legs.py 默认关逐位等价测试守护）：
    # 腿 A（垂直月入场结构盲区，R6-3 实锤 2024-10 池子+9.7% 而 84% 信号 skip_no_pullback
    # 弃单）：等待期无回踩 → 次日开盘市价追入（追入价≥tp2 形态目标透支仍弃）。
    "chase_entry": False,
    # 腿 B（V 反月出场结构盲区，R6-3 实锤 2026-08 58% timeout 仅 10% tp2；R4 H0 线索
    # timeout 组 rr 为正=超期平仓截断正期望单）：超时日浮盈≥门槛 → 一次性延长持有。
    "timeout_extend_days": 0,        # 延长日数（0=关）
    "timeout_extend_min_pnl": 0.05,  # 延长门槛（超时日浮盈比例）
    # R6-10 B3 tp 锚自适应（C 线④ · 2026-08-25）：H/ATR > tp_adapt_h_atr 时
    # tp2/tp1 乘数 ×tp_adapt_scale（V 反修复月对症）。None=关（默认零行为变化）。
    "tp_adapt_h_atr": None,
    "tp_adapt_scale": 0.5,
    # R6-10 L1 时间止损（C 线延伸 · 2026-08-26 用户裁决）：入场 N 个交易日未触发
    # 任何 tp → 离场（持有期分桶单调衰减的结构化兑现）。0=关（默认零行为变化）。
    "time_stop_days": 0,
}


def _accumulate_sim_stats(stats: dict, sim: dict | None) -> None:
    """策略级事件统计累加（A3 · 2026-08-03 Phase A 归因基础设施）。

    物理意图：simulate_exit 返回的 same_day_both / stop_gap 是"逐信号"方向性
    假设标记，自主优化管道需要聚合计数才能量化偏差规模：
        - n_skipped：等待期放弃的信号（skip_no_pullback / skip_target_met）；
        - same_day_both：同日"撤单阈值与成交价都触及"（回测按 cancel 优先的假设）；
        - stop_gap：止损触发日跳空低开（open<stop，成交价被下修，回测保守化）。
    stats 由调用方持有（NecklineMethodStrategy.skip_stats），本函数原地累加。
    """
    if sim is None:
        return
    reason = sim.get("exit_reason")
    if reason in ("skip_no_pullback", "skip_target_met"):
        stats["n_skipped"] += 1
        if sim.get("same_day_both"):
            stats["same_day_both"] += 1
    elif reason == "stop_loss" and sim.get("stop_gap"):
        stats["stop_gap"] += 1


def _cost(side: str, qty: float, price: float) -> float:
    """交易成本（金额口径，实盘用）：万三佣金 min5 + 卖0.05%印花 + 0.001%过户。

    物理意图（plan Task 12 · spec §4.8）：实盘下单金额扣费——佣金双边（买+卖各一次）、
    印花税单边（仅卖）、过户费双边（沪市）。min5 是券商最低佣金门槛（小金额单笔 5 元）。

    回测 simulate_exit 用【费率】口径（百分比 pnl，无 notional，不含 min5 门槛）；本函数
    【金额】口径供实盘 build_orders/对账用（含 min5，小资金精确）。两者费率同源 EXEC_DEFAULTS。

    Args:
        side:  "buy"/"sell"（sell 加印花税）。
        qty:   成交股数。
        price: 成交价。
    Returns:
        成本金额（元）。amount = qty×price；commission=max(amount×0.0003, 5)。
    """
    amount = float(qty) * float(price)
    commission = max(amount * EXEC_DEFAULTS["commission_rate"], 5.0)   # 万三 min5
    stamp = amount * EXEC_DEFAULTS["stamp_rate"] if side == "sell" else 0.0
    transfer = amount * EXEC_DEFAULTS["transfer_rate"]
    return commission + stamp + transfer


def _filter_chuangke_kechuang(symbols: list) -> list:
    """过滤创板科创标的（300/301/688/689 前缀），对齐实盘 _load_universe 口径。

    物理意图（plan Task 13 · spec §11）：回测 universe 收窄创板科创——20cm 涨跌幅 + 流动性
    结构更契合颈线法形态学（与实盘 trading.engine._load_universe 同口径）。主板/北交所不在
    该策略可交易池。回测与实盘标的池一致是冠军档套实盘行为一致的前提（否则回测含主板信号、
    实盘无主板 → 冠军档套实盘行为背离）。
    """
    return [s for s in symbols
            if str(s).split(".")[0].startswith(("300", "301", "688", "689"))]


def simulate_exit(sym_df: pd.DataFrame, signal_idx: int, c_star: float,
                  bottom: float, atr_val: float, exec: dict = None, id_cfg: dict = None):
    """挂单回踩进场 + 颈线−ATR 止损 + 分级止盈（执行层参数化版）。

    exec: 执行层参数 dict（见 EXEC_DEFAULTS），None 用默认。
    id_cfg: 识别层参数 dict（读 stop_atr_mult/tp_h_mult），None 用全局 DEFAULTS。
        阶段B：NecklineMethodStrategy 传 self.id_cfg 解耦全局依赖；scripts 调用不传（用默认）。
    执行：① 挂买单@颈线+buy_limit_atr_mult×ATR，max_wait 天内首个 low≤挂单价 成交，
    等待期 high≥撤单阈值(cancel_thresh_mult×H)则撤单；
    ② 成交后止损@颈线−stop_atr_mult×ATR + 止盈@tp1_h_mult×H / tp_h_mult×H，tp1_portion 减仓。
    """
    if exec is None:
        exec = EXEC_DEFAULTS
    if id_cfg is None:
        id_cfg = DEFAULTS
    max_holding = exec["max_holding"]
    max_wait = exec["max_wait"]
    H = c_star - bottom
    if H <= 0:
        return None
    # CR-2：入场价位三件套改调 price_levels 单源（原内联公式已删，防回测/实盘数学分叉）。
    # 参数映射红线（与原内联取参完全同源）：stop_atr_mult/tp_h_mult ← id_cfg（识别层），
    # buy_limit_atr_mult/tp1_h_mult/cancel_thresh_mult ← exec（执行层）；缺参一律回落
    # PRICE_LEVEL_DEFAULTS（C1：旧式 [] 严格取键缺即 KeyError，.get 兜底只在原崩溃
    # 路径上生效，任何非崩溃路径数值逐位不变——golden 钉死）。
    levels = compute_price_levels(
        c_star=c_star, high=H, atr=atr_val,
        stop_atr_mult=id_cfg.get("stop_atr_mult", PRICE_LEVEL_DEFAULTS["stop_atr_mult"]),
        buy_limit_atr_mult=exec.get("buy_limit_atr_mult",
                                    PRICE_LEVEL_DEFAULTS["buy_limit_atr_mult"]),
        tp1_h_mult=exec.get("tp1_h_mult", PRICE_LEVEL_DEFAULTS["tp1_h_mult"]),
        tp_h_mult=id_cfg.get("tp_h_mult", PRICE_LEVEL_DEFAULTS["tp_h_mult"]),
        # cancel_thresh_mult=None 是合法配置（不撤单放飞），不走数值兜底
        cancel_thresh_mult=exec.get("cancel_thresh_mult"),
        tp_adapt_h_atr=exec.get("tp_adapt_h_atr"),
        tp_adapt_scale=exec.get("tp_adapt_scale", 0.5),
    )
    buy_limit = levels.buy_limit   # 挂单价（颈线+N×ATR；exec 恒有值故非 None）
    # 止损基准（颈线−N×ATR，固定；risk_pct 用此基准预告初始风险；持有期 trailing 动态调整见 loop）
    base_stop = levels.stop
    tp1 = levels.tp1               # 第一止盈（颈线+N×H）
    tp2 = levels.tp2               # 第二止盈（颈线+N×H，识别层参数）
    cancel_on = levels.cancel_on   # 撤单阈值（None=不撤单放飞所有信号；否则等待期 high≥此价即撤单防追高）
    # tp1=None（未配置一档）是 price_levels 合法档：落盘字段用 None 保留（round(None) 炸）
    tp1_out = round(tp1, 3) if tp1 is not None else None

    # ① 等回踩成交（用户逻辑修正：等待期 high≥tp1 → 涨幅已兑现，回踩是退潮，撤单）
    wait_end = min(signal_idx + max_wait, len(sym_df) - 1)
    buy_idx = None
    chase = False         # 腿 A 追入标记（entry 记账口径分叉用，见下方 entry 赋值）
    for i in range(signal_idx + 1, wait_end + 1):
        low_i = float(sym_df["low"].iloc[i])
        high_i = float(sym_df["high"].iloc[i])
        # 等待期价格已达撤单阈值 → 涨幅已兑现，回踩是退潮，撤单不买（过滤"猛突破后回踩"陷阱）
        if cancel_on is not None and high_i >= cancel_on:
            # P0-3 事件顺序量化（2026-08-03）：同根 K 线 high≥cancel_on 且 low≤buy_limit
            # 时回测按 cancel 优先（假设先摸高后回踩）。same_day_both=True 标记这类
            # 方向性假设，供报告归因；若真实顺序是先回踩成交，本信号应算成交。
            same_day_both = low_i <= buy_limit
            return {"signal_date": sym_df.index[signal_idx].date(),
                    "exit_reason": "skip_target_met",
                    "avg_pnl_pct": 0.0, "lot1_pnl_pct": 0.0, "lot2_pnl_pct": 0.0,
                    "neckline": round(c_star, 3), "entry": None,
                    "risk_pct": None, "tp1": tp1_out, "tp2": round(tp2, 3),
                    "same_day_both": same_day_both, "stop_gap": False}
        if low_i <= buy_limit:
            buy_idx = i
            break
    if buy_idx is None:
        # R6-5 腿 A（chase_entry，默认 False=原 skip 语义逐位不变）：等待期无回踩不弃单，
        # 改 wait_end 次日开盘市价追入——垂直拉升月对症（R6-3 实锤 2024-10 池子 +9.7%
        # 而 84% 信号 skip_no_pullback 弃单=入场结构盲区）。追入价≥tp2 时形态目标已透支
        # （追=负期望）仍按原语义弃单；数据尾端（wait_end=len-1）无次日可追同弃。
        if (exec.get("chase_entry") and wait_end + 1 <= len(sym_df) - 1
                and float(sym_df["open"].iloc[wait_end + 1]) < tp2):
            buy_idx = wait_end + 1
            chase = True
        else:
            return {"signal_date": sym_df.index[signal_idx].date(),
                    "exit_reason": "skip_no_pullback",
                    "avg_pnl_pct": 0.0, "lot1_pnl_pct": 0.0, "lot2_pnl_pct": 0.0,
                    "neckline": round(c_star, 3), "entry": None,
                    "risk_pct": None, "tp1": tp1_out, "tp2": round(tp2, 3),
                    "same_day_both": False, "stop_gap": False}

    if chase:
        # 腿 A 追入成交价=当根开盘价本身（市价单语义）——不得走 min(buy_limit, open)：
        # 追入场景 open>buy_limit 恒真（无回踩），min 会记成从未成交过的更优挂单价
        # （与 R6-4 幽灵成交同族的「未到达价位记账」错误）。
        entry = float(sym_df["open"].iloc[buy_idx])
    else:
        entry = min(buy_limit, float(sym_df["open"].iloc[buy_idx]))
        # 限价买单成交价：open>buy_limit（盘中回踩）→ 成交 buy_limit；open<=buy_limit（跳空低开）
        # → 成交 open（市价<挂单价，更优）。旧版 entry=buy_limit 高估了跳空低开的买入价。
    end_idx = min(buy_idx + max_holding, len(sym_df) - 1)

    # ② 持有期逐根判 exit（Task 5 · U3 执行单源：改调 decide_exit 纯函数）
    # strangler 等价红线：本循环改前是 simulate_exit:156-199 的内联 trailing + 四分支判定，
    # 现每根构造 state+bar+cfg → decide_exit → 据 action/reason/portion/new_stop 推进 lot1/lot2
    # 状态机 + pnl + exit_reason/exit_pos。decide_exit 已证等价（Task 4），本循环只保证衔接等价。
    #
    # trailing 收敛（spec §4.3）：原内联算 trailing stop（grace/step/floor）已删除，改用
    # decide_exit 内部调 compute_stop_price 返的 dec.new_stop——trailing 数学不再双份（单源）。
    #
    # lot 翻转硬契约（Task 4 I-1）：decide_exit 是无状态纯函数只读 lot1_open/lot2_open，不写 state，
    # 本循环必须据返回显式翻：TP1→lot1_open=False；TP2→lot2_open=False（+lot1_open if open）。
    # 每根传当前 lot1_open/lot2_open 给 state，否则下根 decide_exit 会重复触发同一触发器。
    lot1_open, lot2_open = True, True
    lot1_pnl, lot2_pnl = None, None
    extended = False         # 腿 B 一次性延长标记（timeout_extend，见 TIMEOUT 分支）
    exit_reason = "timeout"
    exit_pos = end_idx   # 默认超时（is_last 或循环自然结束）；stop_loss/tp2 break 时覆盖
    stop_gap = False     # P0-1：止损触发日跳空低开（open<stop）标记（2026-08-03 Phase A）

    # cfg：decide_exit 需要的静态参数（整个持有期不变）。参数映射红线（resolution §7）：
    #   stop_atr_mult ← id_cfg（与原内联 :167 id_cfg["stop_atr_mult"] 同源）；
    #   trailing_*   ← exec（与原内联 :164-168 exec.get 同源）；
    #   tp1_portion  ← exec（TP1 时 portion 传回，仅供调用方观测，本循环据 reason 分支不读 portion）。
    cfg = {
        "stop_atr_mult": id_cfg["stop_atr_mult"],
        "trailing_grace": exec.get("trailing_grace", 0) or 0,
        "trailing_step": exec.get("trailing_step", 0.0) or 0.0,
        "trailing_floor": exec.get("trailing_floor"),
        "tp1_portion": exec["tp1_portion"],
        # R6-10 L1：时间止损（0=关——decide_exit priority 3.5，N 日未触发任何 tp）
        "time_stop_days": exec.get("time_stop_days", 0) or 0,
    }

    # T+1 红线（R5b · 2026-08-25 实锤修复）：A 股现货当日买入不可当日卖出——
    # 出场判定从 buy_idx **次日**起（原 range(buy_idx,...) 允许当日触发 stop/tp =
    # 违规日内循环：实锤 base 3% 笔数/pnl 贡献 10%、tp_h=0.8 达 14%/29%，且违规笔
    # 均收益 4-11% 远高于合规笔——止盈收紧方向的"收益引擎"曾实质依赖此漏洞）。
    # holding_days 维持 i - buy_idx：次日=第 1 个持有日，对齐实盘 pre_open 的
    # (entry, T-1] 交易日计数口径（C9）。
    # 循环上界一次估满（max_holding + 腿 B 一次性延长预算），实际终点由 end_idx 动态
    # 控制——原 for-range 上界在循环开始时求值，腿 B mid-loop 延长 end_idx 不会扩
    # range（首版实现实锤：延长后循环仍停在旧终点 → pnls 全 None → 误返 None）。
    # 预算=0（默认）时 loop_end == end_idx，短路守卫恒不触发，行为逐位不变。
    ext_budget = int(exec.get("timeout_extend_days", 0) or 0)
    loop_end = min(buy_idx + max_holding + ext_budget, len(sym_df) - 1)
    for i in range(buy_idx + 1, loop_end + 1):
        if i > end_idx:
            break   # 未延长（或延长已消化）到达终点：等价原 range 终点
        row = sym_df.iloc[i]
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        holding_days = i - buy_idx
        is_last = (i == end_idx)

        # 每根构造 state（运行时变量）+ bar（当根 K 线）喂 decide_exit。holding 期不需 cancel_on
        # （decide_exit holding 分支不读）。base_stop 不必入 state（decide_exit 调 compute_stop_price
        # 自算 trailing，返 dec.new_stop）。
        state = {
            "phase": "holding",
            "entry": entry,
            "tp1": tp1,
            "tp2": tp2,
            "neckline": c_star,
            "atr": atr_val,
            "holding_days": holding_days,
            "is_last": is_last,
            "lot1_open": lot1_open,
            "lot2_open": lot2_open,
        }
        bar = {"high": high, "low": low, "close": close}

        dec = decide_exit(state, bar, cfg)
        stop = dec.new_stop   # decide_exit 算好的当根 trailing stop（= compute_stop_price 结果）

        # ── 分发（按 dec.action / dec.reason / dec.portion 推进状态机，逐分支对齐原内联）──
        if dec.action is ExitAction.HOLD:
            # 原内联：四分支均未触发 → 自然下一根（lot 状态不变）
            continue

        if dec.action is ExitAction.CLOSE and dec.reason is ExitReason.STOP_LOSS:
            # 优先级1（原 :174-179）：止损全平。lot1/lot2 用当根成交价算 pnl。
            # P0-1 跳空修正（2026-08-03）：若开盘已低于止损（open<stop），真实市价/
            # 限价止损单成交于更差的开盘价附近——回测按 min(stop, open) 保守成交，
            # 不再假设"完美按 stop 价成交"（防回测高估止损保护、Agent 过拟合紧止损）。
            open_i = float(sym_df["open"].iloc[i])
            stop_fill = min(stop, open_i)
            stop_gap = open_i < stop
            lot1_pnl = lot2_pnl = (stop_fill - entry) / entry
            lot1_open = lot2_open = False
            exit_reason = "stop_loss"
            exit_pos = i
            break

        if dec.action is ExitAction.CLOSE and dec.reason is ExitReason.TAKE_PROFIT \
                and dec.portion >= 1.0:
            # 优先级2（原 :180-188）：tp2 全平。lot2 用 tp2 算 pnl；lot1 若仍持用 tp1 同日一并卖。
            # portion==1.0 = TP2 全平（decide_exit priority 2）；对齐原内联 :181-185。
            lot2_pnl = (tp2 - entry) / entry
            lot2_open = False
            if lot1_open:
                # R6-4 幽灵成交修复（2026-08-26 用户裁决「修复」）：lot1「同日一并卖」的
                # 成交价必须是当根真实到达过的价位——high≥tp1 → 按 tp1 记（tp1≤tp2 的
                # 全部常规配置 high≥tp2≥tp1 恒真，逐位不变，golden 钉死）；tp1>tp2（挂远
                # 永不触发）且 high<tp1 时按 tp2 同价平仓——原版按从未到达的 tp1 记账即
                # 幽灵成交（R6-1 实锤：tp1_h_mult=5 档 6590/18445 笔污染，0.7×82.7%+
                # 0.3×20.5%=64.0% 与读数精确吻合，R6a 图谱 tp1_h_mult 2.0+ 档全部受染）。
                # tp1=None（未配置一档）同落 tp2 = 全量 tp2 语义（原版此处 (None-entry)
                # 会 TypeError 的潜伏崩溃一并消除）。
                lot1_exit = tp1 if (tp1 is not None and high >= tp1) else tp2
                lot1_pnl = (lot1_exit - entry) / entry
                lot1_open = False
            exit_reason = "tp2"
            exit_pos = i
            break

        if dec.action is ExitAction.CLOSE and dec.reason is ExitReason.TAKE_PROFIT \
                and dec.portion < 1.0:
            # 优先级3（原 :189-192）：tp1 只卖 lot1，lot2 续持博 tp2。continue 不是 break
            # （对齐原 :192），下根继续判 lot2 的 tp2/stop/timeout。
            lot1_pnl = (tp1 - entry) / entry
            lot1_open = False
            continue

        if dec.action is ExitAction.CLOSE and dec.reason is ExitReason.TIME_STOP:
            # R6-10 L1：时间止损——N 日未触发任何 tp，剩余全量按 close 平（与
            # timeout 同款离场方式；holding_days 天数口径也同（i-buy_idx））
            if lot1_open:
                lot1_pnl = (close - entry) / entry
            if lot2_open:
                lot2_pnl = (close - entry) / entry
            exit_reason = "time_stop"
            exit_pos = i
            break   # 中途出场（与 stop/tp2 同款；timeout 靠 is_last 无需 break，本分支必需）
        if dec.action is ExitAction.CLOSE and dec.reason is ExitReason.TIMEOUT:
            # R6-5 腿 B（timeout_extend，默认 days=0=零行为变化）：超时日浮盈≥门槛且未
            # 延长过 → 一次性延长持有（V 反修复月对症——R6-3 实锤 2026-08 58% timeout
            # 仅 10% tp2，崩跌基底 tp 锚远、持有截断在半山腰；R4 H0 线索 timeout 组 rr
            # 为正=超期平仓截断正期望单）。延长期间 stop/tp1/tp2/trailing 照常判定，
            # 新 is_last 到达再平（一次性，不链式）。数据尾端无余量不延长。
            ext_days = exec.get("timeout_extend_days", 0) or 0
            if (ext_days > 0 and not extended
                    and end_idx < len(sym_df) - 1
                    and (close - entry) / entry >= (exec.get("timeout_extend_min_pnl", 0.05))):
                end_idx = min(end_idx + int(ext_days), len(sym_df) - 1)
                extended = True
                continue
            # 优先级4（原 :193-199）：is_last 超时强制平。lot1/lot2 各用 close 算 pnl。
            # decide_exit TIMEOUT 不判浮盈 threshold（Controller #5：is_last 直接平），
            # pnl 在本循环算（decide_exit 是纯决策不碰 pnl，对齐 Task 4 契约）。
            if lot1_open:
                lot1_pnl = (close - entry) / entry
            if lot2_open:
                lot2_pnl = (close - entry) / entry
            exit_reason = "timeout"
            exit_pos = i   # = end_idx
    if lot1_pnl is None or lot2_pnl is None:
        return None

    # 交易成本扣费（plan Task 12 · spec §4.8）：entry 扣 buy_rate（佣+过）、每 lot exit 各扣
    # sell_rate（佣+印+过，分级两笔各卖=双倍佣）。仿射变换 net=(1+raw)×(1-sell)/(1+buy)-1，
    # 加权可交换（avg_net == net(raw_avg)，故 avg_pnl 字段先变换 lot 再加权即可）。
    # rate=0（零费率 exec）→ factor=1 零回归（向后兼容 Task 12 前的 raw pnl）。
    buy_rate = exec.get("commission_rate", 0) + exec.get("transfer_rate", 0)
    sell_rate = (exec.get("commission_rate", 0) + exec.get("stamp_rate", 0)
                 + exec.get("transfer_rate", 0))
    if buy_rate or sell_rate:
        factor = (1 - sell_rate) / (1 + buy_rate)
        lot1_pnl = (1 + lot1_pnl) * factor - 1
        lot2_pnl = (1 + lot2_pnl) * factor - 1

    avg_pnl = exec["tp1_portion"] * lot1_pnl + (1 - exec["tp1_portion"]) * lot2_pnl
    # exit_price：分级止盈两批不同价，用 avg_pnl 反推加权平均离场价（供适配器 trade dict）
    exit_price_avg = entry * (1 + avg_pnl)
    return {
        "signal_date": sym_df.index[signal_idx].date(),
        "buy_date": sym_df.index[buy_idx].date(),
        "neckline": round(c_star, 3),
        "entry": round(entry, 3),
        "risk_pct": round((entry - base_stop) / entry * 100, 2),  # 初始风险（基准止损 base_stop，trailing 动态前）
        "tp1": tp1_out, "tp2": round(tp2, 3),
        "H_over_ATR": round(H / atr_val, 2) if atr_val > 0 else None,
        "lot1_pnl_pct": round(lot1_pnl * 100, 2),
        "lot2_pnl_pct": round(lot2_pnl * 100, 2),
        "avg_pnl_pct": round(avg_pnl * 100, 2),
        "exit_reason": exit_reason,
        # 阶段B 新增（供 NecklineMethodStrategy trade dict；纯加字段，不改任何出场行为）
        "exit_date": sym_df.index[exit_pos].date(),
        "exit_price": round(exit_price_avg, 3),
        "holding_bars": exit_pos - buy_idx,
        # P0-3 事件顺序量化：本信号等待期是否出现过"同日撤单阈值与成交价都触及"；
        # P0-1 跳空标记：止损触发日 open<stop（成交价被下修）。
        "same_day_both": False,
        "stop_gap": stop_gap,
    }


def dedup_signals(signals, cooldown=COOLDOWN):
    """信号去重：相邻信号（idx 差 < cooldown）只保留第一个。

    物理意图：同一颈线被连续多日触发（滚动 replay 每个突破日都信号），实盘只应
    交易一次。冷却期内重复信号合并，避免同形态多计。
    """
    if not signals:
        return []
    deduped = [signals[0]]
    for sig_idx, res in signals[1:]:
        if sig_idx - deduped[-1][0] >= cooldown:
            deduped.append((sig_idx, res))
    return deduped


def kelly_metrics(pnls, dates, pos_cap=0.05, freq_cap=150):
    """凯利仓位 + 实盘可实现资金曲线年化（param_iter 目标函数）。

    f* = (bp − q) / b（b=盈亏比=平均盈利/平均亏损，p=胜率，q=1−p），约束 [0, 0.5]。

    实盘年化（2026-07-20 修复短窗爆炸 bug）：
        旧版 curve=Π(1+f*×r/100) 在近年高频短窗（创板科创 1250笔/1.5年、f*=0.2）下
        爆炸至 7257%~16495%——根源：凯利满仓复利假设所有信号独立可同时下注，无视
        持仓并发约束与资金容量。改 pos=min(f*, pos_cap) 封顶单笔仓位：
          · 凯利低（<pos_cap）→ 用凯利（信号差少下注，目标函数受惩罚）
          · 凯利高（≥pos_cap）→ 封顶 pos_cap（信号好最多下注这么多，实盘风控上限）
        区分度保留：curve 由每笔 r 分布（胜率×盈亏比）决定，非 f* 单值；f* 仅作信号
        质量参考返回。pos_cap=0.05（5%单笔）对应创板科创 ±20% 涨跌下 ~0.5% 组合单笔
        风险。年化60% = 每笔 5% 仓位平均贡献 +0.56%，实盘可达的真实目标。
    """
    n = len(pnls)
    if n == 0:
        return 0.0, 1.0, 0.0
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]   # 转正值
    if not wins or not losses:
        return 0.0, 1.0, 0.0
    p_win = len(wins) / n
    avg_win = sum(wins) / len(wins) / 100      # 比例
    avg_loss = sum(losses) / len(losses) / 100
    b = avg_win / avg_loss if avg_loss > 0 else 0
    q = 1 - p_win
    kelly = (b * p_win - q) / b if b > 0 else 0
    kelly = max(0.0, min(kelly, 0.5))           # 约束 [0, 0.5]
    pos = min(kelly, pos_cap)                   # 实盘仓位：凯利封顶 pos_cap（防爆+风控）
    # 频率封顶（2026-07-20 二次修复）：复利 curve 在高频（2905笔/1.5年）下即使 pos=5%
    # 仍爆炸至 784%——根源是复利假设所有信号可同时下注，无视持仓并发。按年封顶
    # freq_cap 笔（模拟实盘最大持仓约束下"先到先得"，每年最多复利 freq_cap 笔），
    # curve 受控。freq_cap=150 ≈ 同时持6只×持有10天×年250交易日（实盘持仓约束）。
    pnl_df = pd.DataFrame({"pnl": pnls, "date": pd.to_datetime(dates)}).sort_values("date")
    pnl_df["year"] = pnl_df["date"].dt.year
    sampled = pnl_df.groupby("year").head(freq_cap)
    curve = 1.0
    for r in sampled["pnl"]:
        curve *= (1 + pos * r / 100)
    days = (max(dates) - min(dates)).days
    years = days / 365.25 if days > 0 else 1.0
    ann = curve ** (1 / years) - 1 if curve > 0 else -1.0
    return kelly, curve, ann


def risk_metrics(pnls, dates, pos_cap=0.05, freq_cap=150):
    """凯利仓位 + 年化 + 信号夏普 + 资金曲线最大回撤（param_iter 多目标 score 用）。

    在 kelly_metrics 基础上补两个风险维度（sampling 与 kelly_metrics 完全一致：
    pos=min(kelly,pos_cap) 封顶 + 按年 head(freq_cap) 封顶），供 param_iter 约束式
    目标 score = 夏普/(1+回撤)（ann≥90% 硬门槛内）使用：
      - 信号夏普 = mean(仓位加权逐笔) / std × √(年交易数)
        物理意图：每笔信号的风险调整收益（与 ann 解耦，反映信号质量）。
        √年交易数 把 per-trade 夏普折到年口径（高频信号天然放大）。
      - 最大回撤 = 仓位加权逐笔 cumulative curve 的 max(peak−trough)/peak
        物理意图：实盘资金曲线最坏一段跌幅，压回撤（针对 top1 跨2月 −72% 痛点）。
    返回 (kelly, curve, ann, sharpe, max_dd)；前三个与 kelly_metrics 同源。
    """
    kelly, curve, ann = kelly_metrics(pnls, dates, pos_cap, freq_cap)
    if kelly <= 0 or len(pnls) < 2:
        return kelly, curve, ann, 0.0, 0.0
    pos = min(kelly, pos_cap)
    pnl_df = pd.DataFrame({"pnl": pnls, "date": pd.to_datetime(dates)}).sort_values("date")
    pnl_df["year"] = pnl_df["date"].dt.year
    sampled = pnl_df.groupby("year").head(freq_cap)
    returns = pos * sampled["pnl"] / 100   # 仓位加权逐笔收益（与 curve 同源 sampling）
    # 信号夏普（per-trade × √年交易数）
    std_r = float(returns.std())
    if std_r > 0:
        days = (max(dates) - min(dates)).days
        years = days / 365.25 if days > 0 else 1.0
        trades_per_year = len(returns) / years
        sharpe = float(returns.mean() / std_r * math.sqrt(trades_per_year))
    else:
        sharpe = 0.0
    # 最大回撤（逐笔 cumulative curve，peak→trough）
    cum = (1.0 + returns).cumprod()
    peak = cum.cummax()
    dd = (cum - peak) / peak
    max_dd = float(abs(dd.min())) if len(dd) > 0 else 0.0
    return kelly, curve, ann, sharpe, max_dd


# ============================================================================
# R6-1（2026-08-26 · ROUND_LOG R6-PhaseA 排程①）：识别/执行解耦 + 识别事件缓存
# ============================================================================
# 物理意图：P1 类单维扫描中执行层 ~10 维（max_holding/max_wait/cooldown/buy_limit/
# tp 组/cancel/trailing 等）不改变识别产物，却每次全量重跑识别循环（识别占
# scan_symbol 耗时 >95%，单次全 universe 评估 ~2.5min）。把 scan_symbol 拆两段：
# 段 1 识别循环（exec 无关，三键缓存）+ 段 2 快路径（cancel 守卫重放 + 去重 +
# simulate_exit）——同 id_cfg 换 exec 的二次评估跳过识别，秒级。
#
# exec 对识别产物的污染点全表（拆分正确性的根基，动识别内核时须复核本清单）：
#   1. _post_detect 的 cancel_on close 守卫（exec_cfg["cancel_thresh_mult"]）——
#      exec 参数但信号过滤语义 → 段 1 以守卫中性 exec 调 detect_signal_fast，
#      段 2 用同一公式逐位重放（见 scan_symbol 段 2）；
#   2. Signal.entry_price/exec_params（buy_limit_atr_mult 等在识别装配期算入，
#      detect 内核的 rr 本身纯 id_cfg——entry=c_star、stop/tp 乘数皆识别层键）——
#      段 1 只缓存几何四元组 (neckline, bottom, atr, formed_at)（均为内核 round
#      后定稿值），entry/rr 从不读 Signal 字段：段 2 的 simulate_exit 内部走
#      compute_price_levels 单源（价位数学唯一归宿，CR-2/A5+C1）。
# 红线：method_v0.py / signal.py 逐字节不动（C2 双轨地基）；拆分前后输出逐位
# 一致由 tests/test_r6_id_exec_split.py 三层守护（拆分==一体化参考实现、缓存
# 命中==无缓存、同 id_cfg 异 exec 二次评估命中）。
_ID_CACHE_GEN = "r6-1"   # 识别路径语义变化时手工 bump（老缓存键自动失效）
_ID_CACHE_MAX_EVENTS = 400_000   # 进程内缓存事件总量上限（~50-100MB，防长跑搜索无界增长）

# 模块级缓存（进程内单例，同 discovery/worker.py _WORKER_STATE 范式：worker 进程
# 跨 trial 复用，不随 params pickle 跨进程）。评估单线程，dict 操作 GIL 原子，无锁。
_scan_id_cache: dict = {}   # key → list[(i, (neckline, bottom, atr, formed_at))]，勿原地 mutate
_scan_id_cache_n_events = 0         # 已缓存事件总数（有界驱逐的计数基准）
_scan_id_cache_stats = {"hit": 0, "miss": 0, "evict": 0}

# 守卫中性 exec：cancel_thresh_mult=None 关掉 _post_detect 的 cancel_on close 守卫
# （该守卫在 scan_symbol 段 2 重放）；entry_price/exec_params 装配残值不出段 1。
_GUARD_NEUTRAL_EXEC = {"cancel_thresh_mult": None}


def _stable_cfg_hash(cfg: dict) -> str:
    """识别参数 dict → 稳定指纹（sort_keys JSON → sha256[:16]）。"""
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _sym_data_hash(sym_df) -> str:
    """识别输入内容指纹：high/low/close/volume 四列 + index 行（ATR/极值掩码/衰减
    权重皆由此派生；simulate_exit 独读的 open 不入指纹——段 2 每次现跑不缓存）。
    pd.util.hash_pandas_object 进程内确定（缓存进程生命周期 < pandas 版本变化）。"""
    h = hashlib.sha256()
    rows = pd.util.hash_pandas_object(
        sym_df[["high", "low", "close", "volume"]], index=True).to_numpy()
    h.update(rows.tobytes())
    h.update(str(len(sym_df)).encode("utf-8"))
    return h.hexdigest()[:16]


def _scan_id_cache_state() -> dict:
    """缓存状态快照（diag 提速实测/测试断言用）。"""
    return {"count": len(_scan_id_cache), "events": _scan_id_cache_n_events,
            **_scan_id_cache_stats}


def _clear_scan_id_cache() -> None:
    """清空识别事件缓存与计数器（测试隔离/运维复位用）。"""
    global _scan_id_cache_n_events
    _scan_id_cache.clear()
    _scan_id_cache_n_events = 0
    _scan_id_cache_stats.update(hit=0, miss=0, evict=0)


def _id_cache_put(key, events) -> None:
    """入缓存 + 总量有界驱逐（FIFO，dict 保持插入序）：超上限从最老条目逐出至
    额度内——长跑搜索（数百 id_cfg 变体 × 全 universe）缓存无界增长会吃 worker
    RSS 预算（P2 看门狗 6GB）。驱逐只损速度不损正确性（键完整时 miss 即重算）。"""
    global _scan_id_cache_n_events
    _scan_id_cache[key] = events
    _scan_id_cache_n_events += len(events)
    while _scan_id_cache_n_events > _ID_CACHE_MAX_EVENTS and len(_scan_id_cache) > 1:
        oldest = next(iter(_scan_id_cache))
        _scan_id_cache_n_events -= len(_scan_id_cache.pop(oldest))
        _scan_id_cache_stats["evict"] += 1
    # 单条目即超限（异常巨标的）：len>1 守卫防自逐——保最后一条，宁可占内存不失速


def _identify_events(sym_df, id_cfg):
    """段 1（R6-1 可缓存）：滚动识别循环 → 未去重原始识别事件（exec 无关）。

    与拆分前 scan_symbol 识别段逐行同源（arr/ATR/极值掩码/衰减权重预计算 + 逐日
    detect_signal_fast，P1 fast path 全套注记见 git 历史），唯二差异：
      - exec 位传【守卫中性 exec】（cancel 守卫在段 2 重放）；
      - Signal 只取几何四元组 (neckline, bottom, atr, formed_at)——装配字段
        entry_price/exec_params 含 exec 污染不缓存；atr 是未取整的 ATR 末值
        float(atr_arr[i])（_post_detect 同源，勿改为内核 round 后的 res["atr"]）。
    """
    # 预算全序列 ATR 一次（窗口对齐 id_cfg["window"]，与 scan_at 同源）。
    atr_full = compute_atr(sym_df["high"], sym_df["low"], sym_df["close"], window=id_cfg["window"])
    arr = {
        "high": sym_df["high"].to_numpy(),
        "low": sym_df["low"].to_numpy(),
        "close": sym_df["close"].to_numpy(),
        "volume": sym_df["volume"].to_numpy(),
        "index": sym_df.index,
    }
    atr_arr = atr_full.to_numpy()
    tops_mask = local_extrema_mask(arr["high"], TOPS_WINDOW, kind="max")   # 顶部聚集（TOPS_WINDOW 单源）
    lows_mask = local_extrema_mask(arr["low"], id_cfg["local_extrema_window"], kind="min")
    tau = id_cfg.get("decay_tau")
    decay_weights = None
    if tau and tau > 0:
        decay_weights = decay_weights_of(id_cfg["window"], tau)   # exp(-(n-1-i)/tau) 单源

    events = []
    for i in range(id_cfg["window"], len(sym_df)):
        # 识别统一（U2 · 2026-07-29 Task 3 → P1 2026-08-13 fast path）：与 scan_live/
        # scan_at 同一识别内核（_detect_core_window + _post_detect），仅 exec 位中性化。
        sig = detect_signal_fast(None, arr, i, id_cfg, _GUARD_NEUTRAL_EXEC,
                                 sym_df.index[i], atr_arr,
                                 tops_mask=tops_mask, lows_mask=lows_mask,
                                 decay_weights=decay_weights)
        if sig is not None:
            events.append((i, (sig.neckline, sig.bottom, sig.atr, sig.formed_at)))
    return events


def _cached_identify_events(sym_df, id_cfg):
    """段 1 的缓存包装，三键 = (id_cfg 稳定 hash ‖ 缓存代‖内核身份, 数据内容 hash)。

    内核身份 = detect_signal_fast.__qualname__（调用时解析模块全局）——生产恒定
    （跨 trial 复用即提速来源）；测试 monkeypatch 换桩自动换键，mock 事件不污染
    真路径缓存、反之亦然。env NECKLINE_ID_CACHE=off 零代码旁路（对照/回滚口，
    同 DISCOVERY_OBJECTIVE env 先例）。"""
    if os.getenv("NECKLINE_ID_CACHE", "on").lower() == "off":
        return _identify_events(sym_df, id_cfg)
    key = (_stable_cfg_hash(id_cfg) + "|" + _ID_CACHE_GEN + "|"
           + detect_signal_fast.__qualname__,
           _sym_data_hash(sym_df))
    cached = _scan_id_cache.get(key)
    if cached is not None:
        _scan_id_cache_stats["hit"] += 1
        return cached
    _scan_id_cache_stats["miss"] += 1
    events = _identify_events(sym_df, id_cfg)
    _id_cache_put(key, events)
    return events


def scan_symbol(sym_df, window, exec=None, id_cfg=None):
    """对单标的滚动识别 + 去重 + 模拟，返回成交结果列表与统计。

    exec: 执行层参数（见 EXEC_DEFAULTS），None 用默认。
    id_cfg: 识别层参数（见 DEFAULTS），None 用 {**DEFAULTS, window:window}。
        P1-b（2026-07-21）：参数化识别层，消除与 strategies/NecklineMethodStrategy.scan_at
        的双轨分叉（scan_symbol 此前硬编码全局 DEFAULTS、scan_at 参数化；现两侧都参数化，
        由 test_scan_symbol_matches_strategy 守护一致）。Layer2 #2a（2026-07-23）去全局
        mutation 后：param_iter.run_one 已显式构造 id_cfg 传入（不再靠 DEFAULTS.update
        全局 patch），driver 路径由 NecklineMethodStrategy.scan_at 经 self.id_cfg 传入；
        两侧均显式透传，simulate_exit 亦经 id_cfg 显式接收（不读全局）。
    R6-1（2026-08-26）：拆两段——段 1 识别循环 exec 无关化 + 三键进程内缓存（同
    id_cfg 换 exec 的二次评估跳过识别，识别占耗时 >95%）；段 2 = cancel_on close
    守卫重放（原在 _post_detect 内以 exec 判，公式逐位同款）→ dedup（cooldown 是
    exec 参数但识别语义，去重放段 2）→ simulate_exit。拆分前后输出逐位一致
    （tests/test_r6_id_exec_split.py 守护）。
    """
    if exec is None:
        exec = EXEC_DEFAULTS
    if id_cfg is None:
        id_cfg = {**DEFAULTS, "window": window}

    # —— 段 1（可缓存）：识别循环 → 未去重事件 [(i, (neckline, bottom, atr, formed_at))] ——
    events = _cached_identify_events(sym_df, id_cfg)

    # —— 段 2：exec 语义逐项重放 ——
    # cancel_on close 守卫（D9 识别期预判）：与 method_v0._post_detect 内联版逐位
    # 同式——H/cancel_on/比较三行的运算顺序不得重排（浮点等价红线）；守卫只删行
    # 不重排，对缓存事件按 i 升序过滤 == 原版在识别循环内逐 i 判。
    cancel_thresh = exec.get("cancel_thresh_mult")
    close_arr = sym_df["close"].to_numpy()
    kept = []
    for i, ev in events:
        if cancel_thresh is not None:
            H = ev[0] - ev[1]
            cancel_on = ev[0] + cancel_thresh * H
            if float(close_arr[i]) >= cancel_on:
                continue   # 涨幅已兑现，不产回踩挂单信号（挡冲天突破）
        kept.append((i, ev))
    signals = dedup_signals(kept, cooldown=exec["cooldown"])
    filled = []
    n_skip = 0
    for sig_idx, ev in signals:
        neckline, bottom, atr_val, _formed_at = ev
        # 显式透传 id_cfg（Critical C1 修复·Layer2 #2a 去 mutation 后的正确性补强）：
        # 旧版靠 param_iter.run_one 的 DEFAULTS.update(id_params) 全局 mutation，让
        # simulate_exit 默认 id_cfg=None → 读"已 patch 的全局"。去 mutation 后全局变纯净，
        # 若不显式传，simulate_exit 会退化用 DEFAULTS 默认 stop_atr_mult=1.0/tp_h_mult=2.0，
        # 悄悄丢弃 param_iter 搜到的非默认档（偷改目标函数，违反 spec #2 + golden 零漂移
        # 等价红线——默认参数下 1.0/2.0==DEFAULTS 故 golden 漏报）。此处转发 = 基线 mutation
        # 语义的真等价（与 run_one 旧版 update 后 simulate_exit 读到的值字面相同）。
        sim = simulate_exit(sym_df, sig_idx, neckline, bottom, atr_val,
                            exec=exec, id_cfg=id_cfg)
        if sim is None:
            continue
        if sim["exit_reason"] in ("skip_no_pullback", "skip_target_met"):
            n_skip += 1
        else:
            # 附识别特征供深挖（突破质量/颈线压制/形态深度）。
            # 注意（U2 收尾 · fix Important #1 2026-07-29）：
            # - suppression：detect_signal 返 Signal 不含 suppression（detect_neckline_method
            #   dict 的 debug-only 元数据，仅供 fullscan CSV 归因分析，不参与 simulate_exit/
            #   去重决策）。此处保留 None 占位（backtest/tools/analyze_fullscan 等下游脚本对
            #   dropna 兼容），真实妥协——defer Task 5 在 detect_signal 侧补 Signal 元数据字段，
            #   不在本 fix 范围扩大返回类型。
            # - H_over_ATR：由 simulate_exit 算并保留（sim 本就有，见上方 simulate_exit，
            #   H=c_star-bottom，atr_val=sig.atr，同源），勿覆盖为 None。
            #   下游 analyze_fullscan.py:17 等大量消费 trades["H_over_ATR"].dropna() 做形态深度
            #   分桶——原 implementer 多写一行 sim["H_over_ATR"]=None 把正确值冲掉致 dropna 全失效。
            vol_T = float(sym_df["volume"].iloc[sig_idx])
            vol5 = float(sym_df["volume"].iloc[max(0, sig_idx - 5):sig_idx].mean()) if sig_idx >= 5 else vol_T
            sim["breakout_vol_ratio"] = round(vol_T / vol5, 2) if vol5 > 0 else 0.0
            sim["suppression"] = None
            filled.append(sim)
    return filled, len(signals), n_skip


def main():
    lake_path = "data_lake/a_shares_daily.parquet"
    print(f"加载 {lake_path} ...")
    lake = pd.read_parquet(lake_path)
    window = DEFAULTS["window"]

    # 1. 全市场流动性过滤（amount 千元；近30日均 ≥ 1e5 千元 = 1 亿元，对齐 caisen）
    print("计算流动性（近30日均成交额 ≥ 1 亿元）...")
    syms = lake.index.get_level_values("symbol").unique().tolist()
    syms = _filter_chuangke_kechuang(syms)   # plan Task 13：对齐实盘创板科创池（300/301/688/689）
    tradable = []
    for s in syms:
        try:
            amt_v = float(lake.xs(s, level="symbol")["amount"].tail(30).mean())
        except Exception:
            continue
        if amt_v >= 1e5:   # 1 亿元（千元单位）
            tradable.append((s, amt_v))
    tradable.sort(key=lambda x: x[1], reverse=True)
    print(f"可交易标的: {len(tradable)} 只 / 全市场 {len(syms)}")
    print(f"参数（窗口={window}, 挂单等待={MAX_WAIT}, 超时={MAX_HOLDING}, 冷却={COOLDOWN}）\n")

    # 2. 逐标的扫描（全市场，带进度）
    per_sym = []
    all_filled = []
    for idx, (sym, _) in enumerate(tradable):
        if idx % 50 == 0:
            print(f"  进度 {idx}/{len(tradable)} ({idx/len(tradable)*100:.0f}%) ...", flush=True)
        try:
            sym_df = lake.xs(sym, level="symbol").sort_index()
            filled, n_sig, n_skip = scan_symbol(sym_df, window)
        except Exception:
            continue
        pnls = [r["avg_pnl_pct"] for r in filled]
        wins = sum(1 for p in pnls if p > 0)
        n = len(filled)
        per_sym.append({
            "symbol": sym, "n_signals": n_sig, "n_filled": n, "n_skip": n_skip,
            "win_rate": wins / n if n else 0.0,
            "avg_pnl": sum(pnls) / n if n else 0.0,
            "total_pnl": sum(pnls),
        })
        for r in filled:
            r["symbol"] = sym
        all_filled.extend(filled)

    # 存 csv 供后续分析（标的筛选 / 参数调优）
    os.makedirs("logs", exist_ok=True)
    if all_filled:
        pd.DataFrame(all_filled).to_csv("logs/neckline_fullscan_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(per_sym).to_csv("logs/neckline_fullscan_summary.csv", index=False, encoding="utf-8-sig")
    print(f"\n逐笔明细 → logs/neckline_fullscan_trades.csv ({len(all_filled)} 笔)")
    print(f"各标的汇总 → logs/neckline_fullscan_summary.csv ({len(per_sym)} 只)\n")

    # 3. 汇总
    from collections import Counter
    print(f"=== 颈线法全市场汇总（可交易 {len(tradable)} 只）===")
    total = len(all_filled)
    if total == 0:
        print("无成交信号")
        return
    total_wins = sum(1 for r in all_filled if r["avg_pnl_pct"] > 0)
    all_pnls = [r["avg_pnl_pct"] for r in all_filled]
    print(f"总成交笔数 = {total}")
    print(f"整体胜率   = {total_wins/total*100:.1f}%  ({total_wins}/{total})")
    print(f"平均收益   = {sum(all_pnls)/total:.2f}%")
    # 凯利仓位 + 年化（参数迭代目标函数）
    kelly_f, curve, ann = kelly_metrics(all_pnls, [pd.to_datetime(r["signal_date"]) for r in all_filled])
    print(f"凯利仓位   = {kelly_f*100:.1f}%   资金曲线 = {curve:.2f}   年化 = {ann*100:.1f}%")
    print(f"中位收益   = {sorted(all_pnls)[total//2]:.2f}%")
    print(f"总收益求和 = {sum(all_pnls):.1f}%")

    print(f"\nexit 分布:")
    rc = Counter(r["exit_reason"] for r in all_filled)
    for reason, cnt in rc.most_common():
        sub = [r["avg_pnl_pct"] for r in all_filled if r["exit_reason"] == reason]
        print(f"  {reason:10s}: {cnt:>4}笔 ({cnt/total*100:>4.0f}%)  avg={sum(sub)/len(sub):+.2f}%")

    print(f"\n=== 各标的（按 avg 收益降序）===")
    print(f"{'symbol':<12}{'信号':>5}{'成交':>5}{'放弃':>5}{'胜率':>7}{'均收益':>8}{'总收益':>9}")
    for s in sorted(per_sym, key=lambda x: x["avg_pnl"], reverse=True):
        print(f"{s['symbol']:<12}{s['n_signals']:>5}{s['n_filled']:>5}{s['n_skip']:>5}"
              f"{s['win_rate']*100:>6.0f}%{s['avg_pnl']:>+7.2f}%{s['total_pnl']:>+8.1f}%")

    # 标的级胜率分布（样本≥3 才有意义）
    valid = [s for s in per_sym if s["n_filled"] >= 3]
    if valid:
        wrs = [s["win_rate"] for s in valid]
        profitable = sum(1 for s in valid if s["avg_pnl"] > 0)
        print(f"\n=== 标的级分布（成交≥3笔的 {len(valid)} 只）===")
        print(f"胜率: max={max(wrs)*100:.0f}%  中位={sorted(wrs)[len(wrs)//2]*100:.0f}%  min={min(wrs)*100:.0f}%")
        print(f"avg收益为正: {profitable}/{len(valid)} 只")


if __name__ == "__main__":
    main()
