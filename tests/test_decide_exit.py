# -*- coding: utf-8 -*-
"""decide_exit 纯函数单测（Task 4 · U3 执行单源基石）。

物理定位：
    decide_exit 是从 simulate_exit（strategies/neckline/backtest.py:125-199 持有期循环）
    抽取的【单根判定纯函数】——把 simulate_exit 里每根 K 线的「该不该离场/为何离场」
    分支判定（pending CANCEL_ON / holding STOP_LOSS / TP2 / TP1 / TIMEOUT / HOLD）
    原样搬成 state+bar+cfg → NecklineExitDecision 的纯函数。

    本 task 完成后 decide_exit 是【已测但未挂接】的纯函数——simulate_exit 的逐根循环
    改调 decide_exit 在 Task 5 做（strangler 红线：逻辑零改动抽取，golden 冠军零退化）。
    实盘 stop_loss_monitor 改调 decide_exit 在 Task 9 做。

测试策略（TDD RED→GREEN）：
    对照 simulate_exit:125-199 六个分支逐一构造 (state, bar, cfg)，断言返回的
    NecklineExitDecision 字段（action/reason/portion/new_stop）：
      1. test_decide_exit_cancel_pending：pending 期 high≥cancel_on → CANCEL/CANCEL_ON
         （strangler 等价 simulate_exit:130 `high >= cancel_on`，用 high 摸高即撤，
          不是 close——Controller #3：执行层 pending 是盘中，有 high）
      2. test_decide_exit_stop_loss_trailing：holding 期 low≤trailing_stop → CLOSE/STOP_LOSS
         （优先级1，simulate_exit:174；trailing stop = compute_stop_price）
      3. test_decide_exit_tp2：high≥tp2 → CLOSE/TAKE_PROFIT/portion=1.0（lot1+lot2 全平）
         （优先级2，simulate_exit:180）
      4. test_decide_exit_tp1：high≥tp1（tp2 未到）→ CLOSE/TAKE_PROFIT/portion=tp1_portion
         （优先级3，simulate_exit:189，卖 lot1，lot2 续持 → 调用方应 continue）
      5. test_decide_exit_timeout：is_last → CLOSE/TIMEOUT（不判浮盈 threshold）
         （优先级4，simulate_exit:193 is_last 直接用 close 算 pnl 强制平，无论浮盈）
      6. test_decide_exit_hold：均未触发 → HOLD/NONE/portion=0.0

关键口径对齐（与 simulate_exit:125-199 逐分支等价 · strangler 红线）：
    - pending 期 decide_exit 只判 CANCEL_ON 或 HOLD；成交（low≤buy_limit）由调用方
      simulate_exit 自己判 buy_idx，不是 decide_exit 的决策（Controller #3）。
    - holding 优先级链：low≤trailing_stop > high≥tp2 > high≥tp1 > is_last（Controller #4）。
    - trailing stop = compute_stop_price（参数映射：stop_atr_mult ← cfg，
      grace/step/floor ← cfg，与 simulate_exit:160-173 内联同源 · Controller #6）。
    - TIMEOUT 用 is_last 标志，不判浮盈 threshold（Controller #5：以 strangler 等价为准）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 项目根挂 sys.path（与 test_detect_signal.py 同口径）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from strategies.neckline.execution import (  # noqa: E402
    decide_exit,
    NecklineExitDecision,
    ExitAction,
    ExitReason,
)
from trading.compute.stop import compute_stop_price  # noqa: E402 （trailing 等价基准；stop.py 垫片透传 execution.py 真身）


# ============================================================================
# 公共夹具：基础 cfg / state 工厂（参数取 EXEC_DEFAULTS 默认档，等价 simulate_exit 调用）
# ============================================================================
def _base_cfg(**overrides) -> dict:
    """构造 decide_exit 的 cfg dict（默认档对齐 EXEC_DEFAULTS）。

    decide_exit 需要的键（Controller #1，对照 simulate_exit:125-199 确定）：
        stop_atr_mult:    识别层止损 ATR 倍数（← id_cfg，simulate_exit:117/167）
        trailing_grace:   宽限天数 b（← exec，simulate_exit:164）
        trailing_step:    收紧速度（← exec，simulate_exit:165）
        trailing_floor:   收紧上限（← exec，simulate_exit:168）
        tp1_portion:      lot1 占比（← exec，simulate_exit:215 加权）
        max_holding:      超时持仓日（← exec，simulate_exit:111/149；is_last 由调用方算）
    """
    cfg = {
        "stop_atr_mult": 1.0,
        "trailing_grace": 0,
        "trailing_step": 0.0,
        "trailing_floor": 0.5,
        "tp1_portion": 0.5,
        "max_holding": 15,
    }
    cfg.update(overrides)
    return cfg


def _holding_state(**overrides) -> dict:
    """构造 holding 阶段 state（运行时变量；Controller #2）。

    state 字段：phase/entry/tp1/tp2/cancel_on/neckline/atr/holding_days/is_last
    数值取定：neckline=100, atr=2, entry=102（颈线+1ATR 挂单成交），tp1=110, tp2=120
    （H=10，tp1=颈线+1H=110，tp2=颈线+2H=120，与 simulate_exit 119-120 同口径）。
    """
    state = {
        "phase": "holding",
        "entry": 102.0,
        "tp1": 110.0,
        "tp2": 120.0,
        "cancel_on": None,
        "neckline": 100.0,
        "atr": 2.0,
        "holding_days": 0,
        "is_last": False,
    }
    state.update(overrides)
    return state


# ============================================================================
# 1. pending 期 CANCEL_ON：high≥cancel_on → CANCEL/CANCEL_ON（simulate_exit:130）
# ============================================================================
def test_decide_exit_cancel_pending():
    """pending 期 high 摸到 cancel_on → 撤单（涨幅兑现，回踩是退潮）。

    strangler 等价 simulate_exit:130：
        if cancel_on is not None and float(sym_df["high"].iloc[i]) >= cancel_on:
            return {exit_reason: "skip_target_met", ...}
    用 high（盘中摸高）不是 close——Controller #3：执行层 pending 有 high。
    """
    state = {
        "phase": "pending",
        "cancel_on": 110.0,
    }
    bar = {"high": 111.0, "low": 105.0, "close": 109.0, "open": 106.0}
    cfg = _base_cfg()
    decision = decide_exit(state, bar, cfg)

    assert decision.action is ExitAction.CANCEL
    assert decision.reason is ExitReason.CANCEL_ON
    assert decision.portion == pytest.approx(1.0)
    assert decision.new_stop is None


# ============================================================================
# 2. holding 期 STOP_LOSS：low≤trailing_stop → CLOSE/STOP_LOSS（simulate_exit:174）
# ============================================================================
def test_decide_exit_stop_loss_trailing():
    """holding 期 low 跌穿 trailing stop → 止损平仓（lot1+lot2 全平）。

    strangler 等价 simulate_exit:160-179：
        holding_days = i - buy_idx
        # trailing 内联（= compute_stop_price 同源）
        stop = neckline - eff_mult * atr  (或 base_stop = neckline - stop_atr_mult*atr)
        if low <= stop: → stop_loss break
    优先级1（最先判定），优先于 tp2/tp1/timeout。
    """
    cfg = _base_cfg(stop_atr_mult=1.0, trailing_grace=0, trailing_step=0.0)
    state = _holding_state(holding_days=0)   # grace=0 → stop=base_stop=颈线-1ATR=98
    # 期望 trailing stop = compute_stop_price(100, 2, 0, 1.0, 0, 0, 0.5) = 100 - 1.0*2 = 98
    expected_stop = compute_stop_price(100.0, 2.0, 0, 1.0, 0, 0.0, 0.5)
    assert expected_stop == pytest.approx(98.0)
    bar = {"high": 105.0, "low": 97.0, "close": 98.0, "open": 100.0}  # low=97 ≤ 98 → 止损

    decision = decide_exit(state, bar, cfg)
    assert decision.action is ExitAction.CLOSE
    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.portion == pytest.approx(1.0)
    assert decision.new_stop == pytest.approx(expected_stop)


# ============================================================================
# 3. holding 期 TP2：high≥tp2 → CLOSE/TAKE_PROFIT/portion=1.0（simulate_exit:180）
# ============================================================================
def test_decide_exit_tp2():
    """high 触及 tp2 → lot1+lot2 一并全平（portion=1.0）。

    strangler 等价 simulate_exit:180-188：
        if lot2_open and high >= tp2:
            lot2_pnl = (tp2-entry)/entry; lot2_open=False
            if lot1_open: lot1_pnl=(tp1-entry)/entry; lot1_open=False  # lot1 同日一并卖
            exit_reason="tp2"; break
    portion=1.0 = 全平（lot1+lot2 100%）。优先级2，在 stop_loss 之后、tp1 之前。
    """
    cfg = _base_cfg()
    state = _holding_state(holding_days=0)
    bar = {"high": 121.0, "low": 105.0, "close": 120.0, "open": 106.0}  # high=121≥tp2=120
    decision = decide_exit(state, bar, cfg)

    assert decision.action is ExitAction.CLOSE
    assert decision.reason is ExitReason.TAKE_PROFIT
    assert decision.portion == pytest.approx(1.0)


# ============================================================================
# 4. holding 期 TP1：high≥tp1（tp2 未到）→ CLOSE/TAKE_PROFIT/portion=tp1_portion
#    （simulate_exit:189，卖 lot1，调用方应 continue 持 lot2）
# ============================================================================
def test_decide_exit_tp1():
    """high 触及 tp1（tp2 未到）→ 只卖 lot1，portion=tp1_portion=0.5。

    strangler 等价 simulate_exit:189-192：
        if lot1_open and high >= tp1:
            lot1_pnl=(tp1-entry)/entry; lot1_open=False
            continue   # 注意是 continue 不是 break —— lot2 继续持
    portion=tp1_portion（0.5）= 卖 lot1，调用方见 CLOSE+portion<1.0 应继续循环 lot2。
    优先级3，在 tp2 之后。bar 的 high 必须 < tp2 才走到这里。
    """
    cfg = _base_cfg(tp1_portion=0.5)
    state = _holding_state(holding_days=0)
    # high=115 ≥ tp1=110 但 < tp2=120 → tp1 命中
    bar = {"high": 115.0, "low": 105.0, "close": 113.0, "open": 106.0}
    decision = decide_exit(state, bar, cfg)

    assert decision.action is ExitAction.CLOSE
    assert decision.reason is ExitReason.TAKE_PROFIT
    assert decision.portion == pytest.approx(0.5)


# ============================================================================
# 5. holding 期 TIMEOUT：is_last → CLOSE/TIMEOUT（simulate_exit:193，不判浮盈）
# ============================================================================
def test_decide_exit_timeout():
    """is_last（持有期最后一根）→ 超时强制平，不判浮盈 threshold。

    strangler 等价 simulate_exit:193-199：
        if is_last:
            if lot1_open: lot1_pnl=(close-entry)/entry
            if lot2_open: lot2_pnl=(close-entry)/entry
            exit_reason="timeout"; exit_pos=i
    Controller #5：is_last 直接用 close 算 pnl 强制平，【不判浮盈 threshold】。
    brief 描述「浮盈<threshold」是 plan 不精确——以 strangler 等价为准。
    本测构造 high 既未到 tp1 也未到 tp2（走不到 tp 分支），low 未跌穿 stop（不走止损），
    is_last=True → TIMEOUT。
    """
    cfg = _base_cfg()
    state = _holding_state(holding_days=5, is_last=True)
    # high=108 < tp1=110（tp1 未到），low=100 > stop=98（止损未到），is_last → timeout
    bar = {"high": 108.0, "low": 100.0, "close": 105.0, "open": 101.0}
    decision = decide_exit(state, bar, cfg)

    assert decision.action is ExitAction.CLOSE
    assert decision.reason is ExitReason.TIMEOUT
    assert decision.portion == pytest.approx(1.0)


# ============================================================================
# 6. HOLD：均未触发 → HOLD/NONE/portion=0.0
# ============================================================================
def test_decide_exit_hold():
    """holding 期未触发任何离场条件 → 继续持有。

    构造：low > stop（不止损），high < tp1 < tp2（不止盈），is_last=False（不超时）。
    portion=0.0（不平仓），reason=NONE。
    new_stop 应为 compute_stop_price 算出的当根 trailing stop（供调用方观测/推进）。
    """
    cfg = _base_cfg()
    state = _holding_state(holding_days=0, is_last=False)
    bar = {"high": 108.0, "low": 100.0, "close": 105.0, "open": 101.0}
    decision = decide_exit(state, bar, cfg)

    assert decision.action is ExitAction.HOLD
    assert decision.reason is ExitReason.NONE
    assert decision.portion == pytest.approx(0.0)
    expected_stop = compute_stop_price(100.0, 2.0, 0, 1.0, 0, 0.0, 0.5)
    assert decision.new_stop == pytest.approx(expected_stop)
