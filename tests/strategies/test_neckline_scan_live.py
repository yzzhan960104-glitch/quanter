# -*- coding: utf-8 -*-
"""NecklineMethodStrategy.scan_live 纯识别入口测试（Task 7a / U2 识别单源 Task 3 更新）。

物理定位：
    scan_live 是实盘入口（区别于 scan_at 回测一站式）——只调识别层产 Signal，
    **不调 simulate_exit 推进未来 K 线模拟出场**。实盘出场由二期引擎
    pre_open / stop_loss_monitor 实时做，T-1 晚 _eod 调用时根本没有未来 K 线可用。

识别单源（U2 · 2026-07-29 Task 3）：scan_live 的内联识别段已抽取为
    ``strategies/neckline/method_v0.py::detect_signal``（Task 2 已测），scan_live 改调
    该单一识别源——桩点相应从 ``nm.detect_neckline_method`` 迁到 ``nm.detect_signal``，
    桩返回值从 ``res dict`` 迁到 ``Signal``（或 None）。断言意图不变（核心红线仍守）：
        1. 命中形态：scan_live 返 Signal 列表且 simulate_exit 未被调用
        2. detect_signal 返 None：scan_live 返空 []
        3. 突破日 != 当日 date：由 detect_signal 内部过滤（桩测不到，改由
           tests/test_detect_signal.py 覆盖），scan_live 桩测聚焦识别→Signal→list 链路。

断言三连（TDD 红→绿）：
    1. 命中形态：scan_live 返 Signal 列表且 simulate_exit 未被调用
    2. detect_signal 返 None：scan_live 返空 []
    3. detect_signal 返的突破日 != 当日 date：scan_live 返空 []（只挂当日新信号）
"""
from __future__ import annotations

import sys
from datetime import date

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# 夹具：构造一个 NecklineMethodStrategy（默认参数，注入假 detect_signal / simulate）
# ---------------------------------------------------------------------------
@pytest.fixture
def strategy(monkeypatch):
    """返一个 NecklineMethodStrategy，detect_signal / simulate_exit 已被 monkey。

    调用方通过 strategy._detect_calls / strategy._sim_calls 观察调用计数，通过
    strategy._detect_return / strategy._sim_return 控制桩返回值。

    桩点迁移（U2 · Task 3）：原桩 nm.detect_neckline_method（返 res dict）改为
    nm.detect_signal（返 Signal | None）——对齐 scan_live 改后的单一识别源。
    """
    from strategies import neckline_method as nm

    strat = nm.NecklineMethodStrategy()

    # 桩状态容器（挂在 strat 上，测试可读写）
    strat._detect_return = None  # 默认 None（各 case 按需覆写为 Signal）
    strat._detect_calls = 0
    strat._sim_return = {"exit_reason": "tp2"}
    strat._sim_calls = 0

    def fake_detect_signal(symbol, df_upto, id_cfg, exec_cfg, date):
        strat._detect_calls += 1
        return strat._detect_return

    def fake_simulate_exit(*args, **kwargs):
        strat._sim_calls += 1
        return strat._sim_return

    # 桩点：scan_live 改调 detect_signal（U2 Task 3 后），不再调 detect_neckline_method。
    monkeypatch.setattr(nm, "detect_signal", fake_detect_signal)
    monkeypatch.setattr(nm, "simulate_exit", fake_simulate_exit)
    # Task 7 U5：完整性 gate 已从 scan_live 上提到 data/integrity.filter_universe_by_continuity
    # （策略层零数据代码）。scan_live 现假设 df_upto 已 filter——fixture 无需再桩 gate。
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
    """detect_signal 返命中 Signal → scan_live 应透传 1 条 Signal，且 simulate_exit 零调用。

    桩点（U2 · Task 3）：detect_signal 已是识别单一源（含 ATR/cancel_on/突破过滤/Signal
    装配全闭包），scan_live 拿到现成 Signal 透传成 list——本 case 断言识别→Signal→list
    链路完整且 simulate_exit 零调用（实盘纯识别红线）。
    """
    from strategies.neckline.signal import Signal

    T = pd.Timestamp("2026-07-21")
    strategy._detect_return = Signal(
        symbol="600000.SH",
        formed_at=T,
        breakout_date=T,
        neckline=10.0,
        bottom=9.0,
        entry_price=11.0,   # detect_signal 已算好（颈线+buy_limit_mult×ATR）
        atr=1.0,
    )
    df_upto = _mk_df_upto(T)

    signals = strategy.scan_live("600000.SH", df_upto, T)

    # 红线：simulate_exit 零调用（实盘纯识别不模拟出场）
    assert strategy._sim_calls == 0, "scan_live 不应调用 simulate_exit"
    # detect_signal 调用 1 次
    assert strategy._detect_calls == 1
    # 返回结构：1 条 Signal
    assert isinstance(signals, list)
    assert len(signals) == 1

    sig = signals[0]
    # scan_live 直接透传 detect_signal 返回的 Signal（原对象，零字段改动）
    assert sig is strategy._detect_return
    assert sig.symbol == "600000.SH"
    assert sig.neckline == 10.0
    assert sig.bottom == 9.0
    assert sig.entry_price == 11.0
    assert sig.atr == 1.0
    assert sig.formed_at == T


# ---------------------------------------------------------------------------
# Case 2：detect_signal 返 None → scan_live 返空 []
# ---------------------------------------------------------------------------
def test_scan_live_no_detection_returns_empty(strategy):
    """detect_signal 返 None（窗口不足/无颈线/未突破/cancel_on 命中/非当日突破等）→ scan_live 返 []。"""
    T = pd.Timestamp("2026-07-21")
    strategy._detect_return = None
    df_upto = _mk_df_upto(T)

    signals = strategy.scan_live("600000.SH", df_upto, T)

    assert signals == []
    assert strategy._detect_calls == 1
    assert strategy._sim_calls == 0


# ---------------------------------------------------------------------------
# Case 3：detect_signal 返的突破日 != date —— 由 detect_signal 内部过滤（U2 Task 3 后下沉）
# ---------------------------------------------------------------------------
# 迁移说明（U2 · 2026-07-29 Task 3）：突破日 != date 的过滤逻辑已随内联识别段一并下沉到
# detect_signal（method_v0.py:365-367），scan_live 拿到的 Signal 必然是当日突破。
# 故此 case 的"非当日突破返 []"契约改由 tests/test_detect_signal.py 覆盖（detect_signal
# 桩测），scan_live 桩测聚焦识别单信号→list 透传链路。删除本 case 避免与 detect_signal
# 内部过滤契约重复（桩测不到该路径）。
def test_scan_live_only_today_breakout(strategy):
    """【契约迁移】非当日突破过滤已下沉 detect_signal；scan_live 桩不再测此路径。

    保留空壳标记迁移事实，真正覆盖在 tests/test_detect_signal.py::test_*breakout_filter*。
    """
    # detect_signal 桩返 None（模拟"非当日突破被 detect_signal 内部过滤掉"）→ scan_live 返 []
    T = pd.Timestamp("2026-07-21")
    strategy._detect_return = None
    df_upto = _mk_df_upto(T)

    signals = strategy.scan_live("600000.SH", df_upto, T)

    assert signals == []
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
    """_eod 传 str 形式 date（"2026-07-21"），detect_signal 已完成 ISO 同日过滤返 Signal——
    scan_live 应透传返 1 条。

    迁移说明（U2 · 2026-07-29 Task 3）：C1 类型混淆修复（两侧 ISO 日期字符串归一比较）
    已随内联识别段下沉到 detect_signal（method_v0.py:366），scan_live 拿到的 Signal 必然
    通过了 ISO 同日过滤。本 case 改测"str date 透传到 detect_signal 不影响透传链路"，
    C1 真正的 ISO 过滤覆盖在 tests/test_detect_signal.py。
    """
    from strategies.neckline.signal import Signal

    T_str = "2026-07-21"   # _eod 真实调用约定：date 是 strftime 出来的 str
    strategy._detect_return = Signal(
        symbol="600000.SH",
        formed_at=pd.Timestamp("2026-07-21"),  # detect_signal 返 Timestamp（已过 ISO 同日过滤）
        breakout_date=pd.Timestamp("2026-07-21"),
        neckline=10.0,
        bottom=9.0,
        entry_price=11.0,
        atr=0.5,
    )
    df_upto = _mk_df_upto(pd.Timestamp("2026-07-21"))

    signals = strategy.scan_live("600000.SH", df_upto, T_str)

    # scan_live 透传 detect_signal 返回的 Signal（str date 不影响透传）
    assert len(signals) == 1, "str date 不应影响 detect_signal 已过滤 Signal 的透传"
    assert signals[0].symbol == "600000.SH"
    assert strategy._sim_calls == 0


# ---------------------------------------------------------------------------
# Task 7 U5 gate 下沉：完整性 gate 已从 scan_live 上提到 data/integrity.filter_universe_by_continuity
# ---------------------------------------------------------------------------
# 物理意图（300214.SZ 案例 · memory data-lake-integrity-gap）：lake 缺停牌复牌段时残缺数据
# 误判颈线 8.07→11.86 误判突破产计划。原 gate 内联在 scan_live（per-symbol 自验窗口连续性），
# Task 7 把 gate 上提到 data/integrity 的 universe 级 filter——策略层 scan_live 假设 df_upto
# 已 filter，零数据质量代码；回测/实盘共用同一 filter（数据校验单源）。
#
# 原本文件 3 个 gate 测试（scan_live 窗口含漏采/完整/全停牌跳空 → return [] / 放行）已迁移到
# tests/test_integrity.py::test_filter_universe_*（直接测 filter 函数，不再经 scan_live 间接测）。
# 本文件改为测「scan_live 删 gate 后的透明度契约」——无论 df_upto 是否完整，scan_live 都透传给
# detect_signal（gate 由调用方 _eod/replay 在 universe 级 pre-filter 负责）。

def _mk_window_df(dates):
    """单标的窗口 df（DatetimeIndex，值任意——detect 被 mock 不真算）。"""
    n = len(dates)
    return pd.DataFrame(
        {"high": [10.0] * n, "low": [9.0] * n, "close": [9.5] * n, "volume": [1000] * n},
        index=pd.DatetimeIndex(pd.to_datetime(dates)),
    )


def test_scan_live_no_longer_gates_unjustified_gap(monkeypatch):
    """Task 7 U5：scan_live 删 gate 后，窗口含未解释漏采也直接透传 detect_signal（不再 return []）。

    红线（gate 下沉 trade-off 的负向契约）：scan_live 不再做完整性 gate——调用方（_eod/replay）
    必须先调 filter_universe_by_continuity 过滤。若 scan_live 重新加了 gate，本测试会失败
    （detect_calls 会是空），提醒维护者 gate 应在 universe 级而非 per-symbol。
    """
    from strategies import neckline_method as nm
    strat = nm.NecklineMethodStrategy()
    detect_calls = []
    monkeypatch.setattr(nm, "detect_signal",
                        lambda *a, **kw: detect_calls.append(1) or None)
    df_upto = _mk_window_df(["2024-09-02", "2024-09-03", "2024-09-06"])  # 缺 09-04, 09-05（漏采）

    strat.scan_live("000001.SZ", df_upto, "2024-09-06")

    # scan_live 不再 gate：detect_signal 必被调（gate 责任在调用方的 universe 级 filter）
    assert detect_calls, "scan_live 删 gate 后应直接调 detect_signal（完整性责任上提到调用方）"


def test_scan_live_no_longer_gates_suspend_gap(monkeypatch):
    """Task 7 U5：全停牌跳空窗口也直接透传 detect_signal（scan_live 零数据质量代码）。

    补强负向契约：即使是合法停牌跳空，scan_live 也不做任何窗口检查——所有 df_upto
    一视同仁透传 detect_signal。gate（区分漏采 vs 停牌跳空）完全由 filter 负责。
    """
    from strategies import neckline_method as nm
    strat = nm.NecklineMethodStrategy()
    detect_calls = []
    monkeypatch.setattr(nm, "detect_signal",
                        lambda *a, **kw: detect_calls.append(1) or None)
    df_upto = _mk_window_df(["2024-09-02", "2024-09-05", "2024-09-06"])  # 缺 09-03, 09-04 停牌

    strat.scan_live("000413.SZ", df_upto, "2024-09-06")

    assert detect_calls, "scan_live 零数据质量代码，停牌跳空也直接透传 detect_signal"
