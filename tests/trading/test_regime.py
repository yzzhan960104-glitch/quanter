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
    """每测试清 data_ctx 当日缓存（读湖装配的进程内缓存会跨测试串结果）。"""
    import trading.data_ctx as dctx
    dctx._REGIME_FRAMES_CACHE.clear()
    yield
    dctx._REGIME_FRAMES_CACHE.clear()


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
    st = classify(index_df=_index_df(list(range(360, 100, -1))),
                  daily_df=_daily_df(600, 0.8))
    assert isinstance(st.reason, str) and "MA200" in st.reason
    # classify 纯化后无进程内缓存——同测试两次调用天然独立（IO 缓存在 data_ctx 层）
    st2 = classify(index_df=_index_df(list(range(100, 360))),
                   daily_df=_daily_df(600, 0.3))
    assert "宽度" in st2.reason


def test_classify_is_pure_no_default_lake_read():
    """纯度契约：classify 无默认读湖分支（df 必传——IO 归 data_ctx 装配层，
    tests/test_compute_purity 守卫 compute 零外部 I/O 的语义对齐）。"""
    import inspect
    sig = inspect.signature(classify)
    assert sig.parameters["index_df"].default is inspect.Parameter.empty
    assert sig.parameters["daily_df"].default is inspect.Parameter.empty


def test_data_ctx_regime_frames_cached_per_day(monkeypatch):
    """data_ctx.load_regime_frames 当日缓存：第二次调用不再读湖（读计数=1）。"""
    import trading.data_ctx as dctx
    calls = {"n": 0}

    def fake_read(path, *a, **k):
        calls["n"] += 1
        if "index_daily" in str(path):
            return _index_df(list(range(100, 360)))
        return _daily_df(600, 0.8)

    monkeypatch.setattr("pandas.read_parquet", fake_read, raising=False)
    dctx._REGIME_FRAMES_CACHE.clear()
    f1 = dctx.load_regime_frames()
    f2 = dctx.load_regime_frames()
    assert calls["n"] == 2          # 两湖各读一次
    assert f1 is f2                 # 第二次命中缓存（同对象）


def test_constants_are_dg_g4_pinned():
    """DG-G4 定稿常量钉死（红线：绝不进 TPE/PARAM_SPACE——改值须走 ADR）。"""
    assert MA_WINDOW == 200
    assert BREADTH_THRESHOLD == 0.5
    assert BREADTH_MIN_STOCKS == 500


# ============================================================================
# 阈值边界（A1 spec §2.3 验收门 · 评审 2026-08-15 补齐）——恰等 → BEAR（严格 >）
# ============================================================================
def test_boundary_price_exactly_ma200_is_bear():
    """HS300 末位 close 恰等 MA200（flat 序列）→ 严格大于不成立 → BEAR。

    锁定实现语义 `last_close > last_ma`（regime.py price_ok）：恰等=未站上年线，
    保守停手——不因浮点边界放行。
    """
    idx = _index_df([100.0] * 260)          # 恒平：close=100, MA200=100 恰等
    daily = _daily_df(600, 0.8)             # 宽度腿过
    st = classify(index_df=idx, daily_df=daily)
    assert st.state == "BEAR"
    assert "MA200" in st.reason


def test_boundary_breadth_exactly_half_is_bear():
    """宽度恰 0.5（300/600 above）→ 严格大于不成立 → BEAR（宽度过半才确认）。"""
    idx = _index_df(list(range(100, 360)))  # 指数腿过
    daily = _daily_df(600, 0.5)             # exactly 50%
    st = classify(index_df=idx, daily_df=daily)
    assert st.state == "BEAR"
    assert "宽度" in st.reason
