# -*- coding: utf-8 -*-
"""detect_signal 纯函数单测（Task 2 · U2 识别统一）。

物理定位：
    detect_signal 是从 NecklineMethodStrategy.scan_live（strategies/neckline_method.py
    :218-298）抽取的【纯识别函数】——ATR 全序列预算 → detect_neckline_method（含 R2
    窗口已突破）→ R1 cancel_on close 口径预判 → 当日突破过滤 → 装配完整 Signal。

    本 task 完成后 detect_signal 是【已测但未挂接】的纯函数——scan_live/scan_at/
    scan_symbol 的调用点改接在 Task 3 做（strangler 红线：逻辑零改动抽取，不顺手优化）。

测试策略（TDD RED→GREEN）：
    用 monkeypatch 桩掉 detect_neckline_method（不依赖合成颈线形态真识别，聚焦
    detect_signal 自身的 ATR/cancel_on/当日过滤/装配分支），断言 5 个分支：
      1. 正常路径：detect 返命中 + close<cancel_on + formed_at==date → 返完整 Signal
         （含 entry=颈线+buy_limit_atr_mult×ATR, atr, rr）
      2. cancel_on close 口径（D9）：close ≥ 颈线+cancel_mult×H → 返 None
      3. 窗口已突破（R2）：靠 detect 自己返 None（detect_signal 直接透传 None）
      4. 非当日突破：formed_at != date → 返 None
      5. ATR 窗口对齐：detect_signal 用 id_cfg["window"] 算 ATR（非写死 14）

关键口径对齐（与 scan_live 源逻辑逐位一致 · strangler 等价红线）：
    - cancel_on 守卫用【close】（不是 high）——scan_live:257 源已是 close 口径，
      detect_signal 继承（Controller resolution #2：不需要改 high→close）。
    - entry_price = (neckline or 0) + buy_limit_atr_mult × ATR末值（atr 缺失/NaN 回退
      res["atr"]）——与 scan_live:289-290 同口径。
    - ATR 全序列预算后取 .iloc[-1]（末根对齐 date 当日）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# 项目根挂 sys.path（与 test_neckline_core.py 同口径）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from strategies.neckline.method_v0 import detect_signal, DEFAULTS, compute_atr  # noqa: E402
from strategies.neckline.backtest import EXEC_DEFAULTS  # noqa: E402
from strategies.neckline import method_v0 as nm_v0  # noqa: E402  （monkeypatch 入口）
from strategies.neckline.signal import Signal  # noqa: E402


# ============================================================================
# 夹具：合成 df_upto（OHLCV，index 末根 == date）
# ============================================================================
def _mk_df_upto(T: pd.Timestamp, n: int = 60) -> pd.DataFrame:
    """造最小可用 df_upto（OHLCV，DatetimeIndex 末根 == T）。

    n 根够 compute_atr（window 默认 60）；值任意——detect_neckline_method 已被桩替换
    不会真算识别，但 compute_atr 会真算 ATR（detect_signal 显式调它做窗口对齐预算）。
    故 high/low/close 值会影响 ATR 末值断言——本夹具取固定值让 ATR 可预测。
    """
    idx = pd.date_range(end=T, periods=n, freq="D")
    return pd.DataFrame(
        {
            "high": [10.0] * n,
            "low": [9.0] * n,
            "close": [9.5] * n,
            "volume": [1000] * n,
        },
        index=idx,
    )


@pytest.fixture
def stub_detect(monkeypatch):
    """桩 detect_neckline_method：调用方通过 stub.return_value / stub.calls 控制+观察。

    Why 桩：detect_neckline_method 的合成形态测试已在 test_neckline_core.py 等覆盖，
    本文件聚焦 detect_signal 自身的 ATR 预算/cancel_on/当日过滤/Signal 装配分支——
    detect 返回值由桩精确控制，避免合成颈线形态的不确定性污染分支断言。
    """
    stub = type("S", (), {"return_value": {"dummy": True}, "calls": []})()

    def fake_detect(df_upto, id_cfg, atr_series=None):
        stub.calls.append({"id_cfg": id_cfg, "atr_series_tail": atr_series.iloc[-1] if atr_series is not None else None})
        return stub.return_value

    monkeypatch.setattr(nm_v0, "detect_neckline_method", fake_detect)
    return stub


# ============================================================================
# Case 1：正常路径 → 返完整 Signal（entry/atr/rr 字段装配）
# ============================================================================
def test_detect_signal_normal(stub_detect):
    """标准颈线突破 → detect_signal 返 Signal（entry=颈线+buy_limit_mult×ATR, atr, rr）。

    断言字段逐位对齐 scan_live:279-299 装配口径（strangler 等价红线）：
        - symbol/signal_type/formed_at/breakout_date/neckline/bottom 直接透传
        - entry_price = (neckline or 0) + buy_limit_atr_mult × ATR末值
        - atr = ATR末值（atr_full.iloc[-1]）
        - rr = res["rr"]（R3 实际口径透传）
    """
    T = pd.Timestamp("2026-07-21")
    stub_detect.return_value = {
        "formed_at": T,
        "neckline": 10.0,
        "bottom": 9.0,
        "entry": 10.0,
        "atr": 0.5,
        "rr": 2.0,
    }
    df_upto = _mk_df_upto(T)

    sig = detect_signal(
        "600000.SH", df_upto,
        id_cfg=dict(DEFAULTS),
        exec_cfg=dict(EXEC_DEFAULTS),
        date=T,
    )

    # 正常路径必须返 Signal（非 None）
    assert sig is not None, "正常识别应返 Signal"
    assert isinstance(sig, Signal)
    # 识别几何字段透传
    assert sig.symbol == "600000.SH"
    assert sig.signal_type == "neckline"
    assert sig.formed_at == T
    assert sig.breakout_date == T
    assert sig.neckline == 10.0
    assert sig.bottom == 9.0
    assert sig.rr == 2.0
    # entry_price = 颈线 + buy_limit_atr_mult × ATR末值
    # df_upto 固定 high=10/low=9/close=9.5 → TR=1（无跳空）→ ATR(window=60)=1.0
    atr_end = float(compute_atr(df_upto["high"], df_upto["low"], df_upto["close"], window=DEFAULTS["window"]).iloc[-1])
    expected_entry = 10.0 + EXEC_DEFAULTS["buy_limit_atr_mult"] * atr_end
    assert sig.entry_price == pytest.approx(expected_entry), (
        f"entry_price={sig.entry_price} ≠ 颈线+buy_limit_mult×ATR={expected_entry}")
    # atr = ATR末值
    assert sig.atr == pytest.approx(atr_end)


# ============================================================================
# Case 2：cancel_on close 口径（D9）→ 返 None
# ============================================================================
def test_detect_signal_cancel_on_close(stub_detect):
    """close ≥ 颈线 + cancel_thresh_mult × H → 返 None（D9 close 口径守卫）。

    物理意图（挡缺口1+4）：close 已远超颈线（冲天突破），挂颈线回踩买单永不成交或
    反抽顶高风险——识别期 close 守卫直接拦下，不产信号。

    口径（Controller resolution #2）：scan_live:257 源已是【close】口径（非 high），
    detect_signal 继承 close 判定（不需要从 high 改 close，那只是回测 simulate_exit 侧）。
    """
    T = pd.Timestamp("2026-07-21")
    # 颈线=10 / bottom=9 → H=1；cancel_thresh_mult=1.0（默认）→ cancel_on = 10+1×1 = 11
    # 构造 close_T=12 ≥ 11 → 触发 close 守卫返 None
    stub_detect.return_value = {
        "formed_at": T,
        "neckline": 10.0,
        "bottom": 9.0,
        "entry": 10.0,
        "atr": 0.5,
        "rr": 2.0,
    }
    df_upto = _mk_df_upto(T)
    # 覆盖末根 close 为 12（≥ cancel_on=11）
    df_upto.loc[df_upto.index[-1], "close"] = 12.0

    sig = detect_signal(
        "600000.SH", df_upto,
        id_cfg=dict(DEFAULTS),
        exec_cfg=dict(EXEC_DEFAULTS),
        date=T,
    )

    assert sig is None, "close≥cancel_on 应被识别期 close 守卫拦下返 None（D9）"


# ============================================================================
# Case 3：窗口已突破（R2）→ 返 None（靠 detect 自己返 None）
# ============================================================================
def test_detect_signal_window_broken(stub_detect):
    """detect_neckline_method 返 None（窗口内已突破等 R2 场景）→ detect_signal 透传 None。

    物理意图：R2 窗口已突破的判定在 detect 内部（detect 含 R2 逻辑），detect_signal
    只是上层调度——detect 返 None 时 detect_signal 直接返 None，不装配 Signal。
    """
    T = pd.Timestamp("2026-07-21")
    stub_detect.return_value = None  # detect 自己判定窗口已突破返 None
    df_upto = _mk_df_upto(T)

    sig = detect_signal(
        "600000.SH", df_upto,
        id_cfg=dict(DEFAULTS),
        exec_cfg=dict(EXEC_DEFAULTS),
        date=T,
    )

    assert sig is None, "detect 返 None（窗口已突破）→ detect_signal 应返 None"


# ============================================================================
# Case 4：非当日突破（formed_at != date）→ 返 None
# ============================================================================
def test_detect_signal_not_today(stub_detect):
    """formed_at != date（历史形态，非当日突破）→ 返 None（只挂当日新信号）。

    物理意图：实盘只挂当日新信号，避免重发历史信号占仓；历史形态的仓位由
    eod_plan 状态机跟踪，不靠 detect_signal 重吐。

    类型对齐（C1 final-fix）：detect 返 formed_at 是 Timestamp，date 可能是 str
    （_eod 真实调用约定），detect_signal 内部必须两侧统一 ISO 日期字符串比较，
    否则 Timestamp != str 恒 True → 所有真实信号被误判为历史信号丢弃（实盘静默死亡）。
    """
    T = pd.Timestamp("2026-07-21")
    yesterday = T - pd.Timedelta(days=1)
    stub_detect.return_value = {
        "formed_at": yesterday,   # 突破日是昨天
        "neckline": 10.0,
        "bottom": 9.0,
        "entry": 10.0,
        "atr": 0.5,
        "rr": 2.0,
    }
    df_upto = _mk_df_upto(T)

    sig = detect_signal(
        "600000.SH", df_upto,
        id_cfg=dict(DEFAULTS),
        exec_cfg=dict(EXEC_DEFAULTS),
        date=T,
    )

    assert sig is None, "非当日突破（formed_at != date）应返 None"


# ============================================================================
# Case 5：ATR 窗口对齐（用 id_cfg["window"] 非写死 14）
# ============================================================================
def test_detect_signal_atr_window_aligned(stub_detect):
    """detect_signal 算 ATR 用 id_cfg["window"]（非写死 14）——窗口对齐红线。

    物理意图（scan_live:218-223 注释）：颈线在 window 天形成，衡量其波动尺度也用
    window 天——而非写死 14 天短期 ATR。detect_signal 调 compute_atr 时必须把
    id_cfg["window"] 透传，否则 ATR 尺度与颈线识别窗口脱节。

    验证方式：用非默认 window（30，默认是 60）调 detect_signal，捕获传给 detect 的
    atr_series 末值，对比 compute_atr(window=30) vs compute_atr(window=60) 的末值
    差异——若 detect_signal 写死 14 或 60 则末值会错。
    """
    T = pd.Timestamp("2026-07-21")
    stub_detect.return_value = {
        "formed_at": T,
        "neckline": 10.0,
        "bottom": 9.0,
        "entry": 10.0,
        "atr": 0.5,
        "rr": 2.0,
    }
    df_upto = _mk_df_upto(T, n=60)  # 60 根够 window=30 和 window=60

    # 非默认 window=30
    id_cfg_w30 = {**DEFAULTS, "window": 30}
    detect_signal(
        "600000.SH", df_upto,
        id_cfg=id_cfg_w30,
        exec_cfg=dict(EXEC_DEFAULTS),
        date=T,
    )

    # 捕获传给 detect 的 atr_series 末值
    captured_atr_tail = stub_detect.calls[-1]["atr_series_tail"]
    # 预期：compute_atr 用 window=30 算出的末值
    expected_atr_tail = float(
        compute_atr(df_upto["high"], df_upto["low"], df_upto["close"], window=30).iloc[-1]
    )
    # 对照：若写死 window=60 算出的末值（与本夹具固定值相同，但用计算值证尺度对齐）
    atr_w60 = float(
        compute_atr(df_upto["high"], df_upto["low"], df_upto["close"], window=60).iloc[-1]
    )
    # 本夹具 high/low/close 固定 → TR 全=1 → ATR 任意 window 都=1.0，故需用一个能区分
    # window 的断言：直接断言 captured == expected(window=30)，且 detect_signal 调了
    # compute_atr（calls 非空证明走了预算路径）。
    assert captured_atr_tail is not None, "detect_signal 应预算 ATR 全序列并传给 detect"
    assert captured_atr_tail == pytest.approx(expected_atr_tail), (
        f"atr_series 末值={captured_atr_tail} ≠ compute_atr(window=30) 末值={expected_atr_tail}"
        "（detect_signal 应透传 id_cfg['window']=30 给 compute_atr，非写死）"
    )
    # 强区分：构造一个 ATR 随 window 变化的 df（末根 TR 独大），再验 window=14 vs 30 末值不同
    df_var = _mk_df_upto(T, n=60).copy()
    # 末根 high 拉高 → 末根 TR 独大 → ATR(window) 是 TR 的窗口均值，window 越大末值越被旧 TR 拉低
    df_var.loc[df_var.index[-1], "high"] = 100.0
    detect_signal(
        "600000.SH", df_var,
        id_cfg=id_cfg_w30,
        exec_cfg=dict(EXEC_DEFAULTS),
        date=T,
    )
    captured_var = stub_detect.calls[-1]["atr_series_tail"]
    expected_var_w30 = float(
        compute_atr(df_var["high"], df_var["low"], df_var["close"], window=30).iloc[-1]
    )
    expected_var_w14 = float(
        compute_atr(df_var["high"], df_var["low"], df_var["close"], window=14).iloc[-1]
    )
    assert captured_var == pytest.approx(expected_var_w30), (
        f"可变 ATR 场景：captured={captured_var} ≠ window=30 末值={expected_var_w30}"
    )
    # 确认 window=30 vs 14 末值确实不同（否则本断言无区分力）
    assert expected_var_w30 != pytest.approx(expected_var_w14), (
        "测试设计问题：window=30 与 14 末值应不同才有区分力"
    )
