# -*- coding: utf-8 -*-
"""A1 regime 闸单测（DG-G4：HS300>MA200 ∧ 宽度>0.5 双确认，UNKNOWN fail-closed）。

物理意图：颈线法冠军参数 2022 熊市折外 calmar=-0.62（wf 四折实证），空头环境
假突破多——闸必须在空头/数据缺失时挡住新单。本文件锁 classify 三态语义与
阈值边界；engine 接入侧见 test_engine_regime_gate.py。
"""
import pandas as pd
import pytest

from trading.compute.regime import (
    classify, RegimeState, MA_WINDOW, BREADTH_THRESHOLD, BREADTH_MIN_STOCKS,
)


def _index_df(closes):
    """合成单标的指数 df（index=[date,symbol]，对齐 index_daily 湖 schema）。"""
    idx = pd.MultiIndex.from_product(
        [pd.bdate_range("2020-01-01", periods=len(closes)), ["000300.SH"]],
        names=["date", "symbol"])
    return pd.DataFrame({"close": closes}, index=idx)


def _daily_df(n_symbols, above_frac, n_days=220):
    """合成宽度数据：n_symbols 只标的各 n_days 根；above_frac 比例的标的
    持续上移（末位 close>自身 MA200），其余持续下移（末位 <MA200）。
    时点宽度语义（末位行判定）下的确定性构造。
    性能：日期数组循环外一次生成（内层逐行 bdate_range 是 2900 万次构造的
    fixture 灾难，测试 120s 超时的根因）。"""
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    rows = []
    n_above = int(n_symbols * above_frac)
    for i in range(n_symbols):
        sym = f"{600000 + i}.SH"
        drift = 0.05 if i < n_above else -0.05   # above 恒上行 / below 恒下行
        base = [100 + drift * k for k in range(n_days)]
        rows.extend((dates[k], sym, base[k]) for k in range(n_days))
    df = pd.DataFrame(rows, columns=["date", "symbol", "close"])
    return df.set_index(["date", "symbol"])


@pytest.fixture(autouse=True)
def _clear_cache():
    """每测试清当日缓存（classify 的进程内 _CACHE 会跨测试串结果）。"""
    from trading.compute import regime
    regime._CACHE.clear()
    yield
    regime._CACHE.clear()


def test_bull_when_price_above_ma200_and_breadth_confirms():
    """双确认全过 → BULL（可交易）。"""
    idx = _index_df(list(range(100, 100 + 260)))   # 严格上行 → 末位 close>MA200
    daily = _daily_df(600, 0.8)                     # 时点宽度 80% > 0.5
    st = classify(index_df=idx, daily_df=daily)
    assert st.state == "BULL"


def test_bear_when_price_below_ma200():
    """指数腿失败（close≤MA200）→ BEAR（宽度再好也停手）。"""
    idx = _index_df(list(range(360, 100, -1)))     # 严格下行
    daily = _daily_df(600, 0.8)
    st = classify(index_df=idx, daily_df=daily)
    assert st.state == "BEAR"


def test_bear_when_breadth_fails():
    """宽度腿失败（≤0.5）→ BEAR（防指数靠权重股撑的假多头）。"""
    idx = _index_df(list(range(100, 360)))         # 指数多头
    daily = _daily_df(600, 0.3)                     # 时点宽度 30%
    st = classify(index_df=idx, daily_df=daily)
    assert st.state == "BEAR"


def test_unknown_when_index_history_too_short():
    """指数 <MA_WINDOW+1 根 → UNKNOWN（fail-closed 由调用方执行停手）。"""
    idx = _index_df(list(range(100, 300)))         # 200 根：MA200 在末位恰好 NaN
    daily = _daily_df(600, 0.8)
    st = classify(index_df=idx, daily_df=daily)
    assert st.state == "UNKNOWN"


def test_unknown_when_breadth_sample_too_small_and_price_ok():
    """宽度样本 < BREADTH_MIN_STOCKS（数据残缺）→ UNKNOWN（指数腿好也不放行——
    缺信息时收紧）。"""
    idx = _index_df(list(range(100, 360)))
    daily = _daily_df(100, 0.8)                     # 仅 100 只 < 500
    st = classify(index_df=idx, daily_df=daily)
    assert st.state == "UNKNOWN"


def test_reason_is_human_readable_with_numbers():
    """reason 人读中文含具体数字（播报/日志定位用）。"""
    from trading.compute import regime
    st = classify(index_df=_index_df(list(range(360, 100, -1))),
                  daily_df=_daily_df(600, 0.8))
    assert isinstance(st.reason, str) and "MA200" in st.reason
    # 同测试内两次 classify 的缓存键同为 "latest"——第二次调用前清缓存防劫持
    regime._CACHE.clear()
    st2 = classify(index_df=_index_df(list(range(100, 360))),
                   daily_df=_daily_df(600, 0.3))
    assert "宽度" in st2.reason


def test_same_day_cached_without_df():
    """当日缓存：同基准日第二次调用免注入命中缓存（不重读 455MB 湖）。"""
    idx = _index_df(list(range(100, 360)))
    daily = _daily_df(600, 0.8)
    s1 = classify(index_df=idx, daily_df=daily)
    s2 = classify()                                 # 无注入 → 必须走缓存（否则读湖）
    assert s1.asof == s2.asof and s1.state == s2.state


def test_constants_are_dg_g4_pinned():
    """DG-G4 定稿常量钉死（红线：绝不进 TPE/PARAM_SPACE——改值须走 ADR）。"""
    assert MA_WINDOW == 200
    assert BREADTH_THRESHOLD == 0.5
    assert BREADTH_MIN_STOCKS == 500
