# -*- coding: utf-8 -*-
"""NecklineMethodStrategy.scan_live 纯识别入口测试（Task 7a）。

物理定位：
    scan_live 是实盘入口（区别于 scan_at 回测一站式）——只调 detect_neckline_method
    识别形态，**不调 simulate_exit 推进未来 K 线模拟出场**。实盘出场由二期引擎
    pre_open / stop_loss_monitor 实时做，T-1 晚 _eod 调用时根本没有未来 K 线可用。

断言三连（TDD 红→绿）：
    1. 命中形态：scan_live 返 Signal 列表且 simulate_exit 未被调用
    2. detect 返 None：scan_live 返空 []
    3. detect 返的突破日 != 当日 date：scan_live 返空 []（只挂当日新信号）
"""
from __future__ import annotations

import sys
from datetime import date

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# 夹具：构造一个 NecklineMethodStrategy（默认参数，注入假 detect / simulate）
# ---------------------------------------------------------------------------
@pytest.fixture
def strategy(monkeypatch):
    """返一个 NecklineMethodStrategy，detect_neckline_method / simulate_exit 已被 monkey。

    调用方通过 strategy._detect_calls / strategy._sim_calls 观察调用计数，通过
    strategy._detect_return / strategy._sim_return 控制桩返回值。
    """
    from strategies import neckline_method as nm

    strat = nm.NecklineMethodStrategy()

    # 桩状态容器（挂在 strat 上，测试可读写）
    strat._detect_return = {"dummy": True}
    strat._detect_calls = 0
    strat._sim_return = {"exit_reason": "tp2"}
    strat._sim_calls = 0

    def fake_detect(df_upto, id_cfg, atr_series=None):
        strat._detect_calls += 1
        return strat._detect_return

    def fake_simulate_exit(*args, **kwargs):
        strat._sim_calls += 1
        return strat._sim_return

    monkeypatch.setattr(nm, "detect_neckline_method", fake_detect)
    monkeypatch.setattr(nm, "simulate_exit", fake_simulate_exit)
    return strat


def _mk_df_upto(T: pd.Timestamp) -> pd.DataFrame:
    """造一个最小可用的 df_upto（OHLCV，index 末根 == T），仅供 scan_live 内部 ATR 调用。"""
    # 60 根够 compute_atr（window 默认 60）；值任意——detect 已被桩替换不会真算
    idx = pd.date_range(end=T, periods=60, freq="D")
    return pd.DataFrame(
        {
            "high": [10.0] * 60,
            "low": [9.0] * 60,
            "close": [9.5] * 60,
            "volume": [1000] * 60,
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# Case 1：命中形态 → 返 Signal 列表，且 simulate_exit 未被调用（核心红线）
# ---------------------------------------------------------------------------
def test_scan_live_returns_signal_without_simulate_exit(strategy):
    """detect 返命中 + 当日突破 → scan_live 应返 1 条 Signal，且 simulate_exit 零调用。"""
    T = pd.Timestamp("2026-07-21")
    strategy._detect_return = {
        "formed_at": T,           # detect 末根突破日（= df_upto.index[-1]）
        "neckline": 10.0,
        "bottom": 9.0,
        "entry": 10.0,            # 进场价（= 颈线价 c_star）
        "atr": 0.5,
    }
    df_upto = _mk_df_upto(T)

    signals = strategy.scan_live("600000.SH", df_upto, T)

    # 红线：simulate_exit 零调用（实盘纯识别不模拟出场）
    assert strategy._sim_calls == 0, "scan_live 不应调用 simulate_exit"
    # detect 调用 1 次
    assert strategy._detect_calls == 1
    # 返回结构：1 条 Signal
    assert isinstance(signals, list)
    assert len(signals) == 1

    sig = signals[0]
    # Layer2 阶段1：scan_live 返 list[Signal]（frozen dataclass），读属性
    assert sig.symbol == "600000.SH"
    assert sig.neckline == 10.0
    assert sig.bottom == 9.0
    # entry_price 取 res["entry"]，缺则用 neckline 近似（此处 res 有 entry）
    assert sig.entry_price == 10.0
    # atr 用 atr_full 末值（df_upto 全 ATR 末根）
    assert sig.atr is not None
    # formed_at / breakout_date 字段供 signal_runner 消费
    assert sig.formed_at == T


# ---------------------------------------------------------------------------
# Case 2：detect 返 None → scan_live 返空 []
# ---------------------------------------------------------------------------
def test_scan_live_no_detection_returns_empty(strategy):
    """detect 返 None（窗口不足/无颈线/未突破等）→ scan_live 返 []。"""
    T = pd.Timestamp("2026-07-21")
    strategy._detect_return = None
    df_upto = _mk_df_upto(T)

    signals = strategy.scan_live("600000.SH", df_upto, T)

    assert signals == []
    assert strategy._detect_calls == 1
    assert strategy._sim_calls == 0


# ---------------------------------------------------------------------------
# Case 3：detect 返的突破日 != date → 只返当日突破，非当日返 []
# ---------------------------------------------------------------------------
def test_scan_live_only_today_breakout(strategy):
    """detect 返的 formed_at != date（历史形态，非当日突破）→ scan_live 返 []。

    物理意图：实盘只挂当日新信号（避免重发历史信号占仓）；历史形态的仓位
    由 eod_plan 状态机跟踪，不靠 scan_live 重吐。
    """
    T = pd.Timestamp("2026-07-21")
    yesterday = T - pd.Timedelta(days=1)
    strategy._detect_return = {
        "formed_at": yesterday,   # 突破日是昨天，不是今天
        "neckline": 10.0,
        "bottom": 9.0,
        "entry": 10.0,
        "atr": 0.5,
    }
    df_upto = _mk_df_upto(T)

    signals = strategy.scan_live("600000.SH", df_upto, T)

    assert signals == [], "非当日突破不应吐信号"
    assert strategy._sim_calls == 0


# ---------------------------------------------------------------------------
# Case 4（C1 · final-fix）：_eod 真实调用约定——date 传 str，detect 返 Timestamp
# ---------------------------------------------------------------------------
# 物理意图（C1 缺陷）：scan_live 内 `breakout_date != date` 比较，左侧 res["formed_at"]
# 是 pd.Timestamp（df_upto 的 DatetimeIndex），右侧是 _eod 传来的 str
# （datetime.now().strftime("%Y-%m-%d")）。pandas 的 __ne__ 不像 __eq__ 做字符串解析，
# Timestamp != str 恒 True → 过滤器总触发 → 所有真实信号被当历史信号丢弃 →
# 实盘静默死亡（_eod 从不产信号，从不交易）。
#
# 修复契约：比较前统一两侧类型为 ISO 日期字符串，Timestamp 一致日 str 不再被误判。
# 本 case 直接复刻 _eod 真实调用约定（date 是 str），是修复前的回归红线。
def test_scan_live_with_string_date_from_eod(strategy):
    """_eod 传 str 形式 date（"2026-07-21"），detect 返 Timestamp——应仍能识别为当日突破。

    修复前：breakout_date（Timestamp）!= date（str）恒 True → 信号被丢弃 → 返 0（bug）。
    修复后：两侧统一 ISO 日期字符串比较 → 返 1 条（绿）。
    """
    T_str = "2026-07-21"   # _eod 真实调用约定：date 是 strftime 出来的 str
    strategy._detect_return = {
        "formed_at": pd.Timestamp("2026-07-21"),  # detect 返的是 Timestamp（df_upto DatetimeIndex）
        "neckline": 10.0,
        "bottom": 9.0,
        "entry": 10.0,
        "atr": 0.5,
    }
    df_upto = _mk_df_upto(pd.Timestamp("2026-07-21"))

    signals = strategy.scan_live("600000.SH", df_upto, T_str)

    # 修复前：len == 0（bug · 实盘静默死亡）
    # 修复后：len == 1（当日突破被正确识别）
    assert len(signals) == 1, "str date 与 Timestamp formed_at 同日应识别为当日突破（C1）"
    assert signals[0].symbol == "600000.SH"
    assert strategy._sim_calls == 0
