# -*- coding: utf-8 -*-
"""因子网格扩返契约：分层累计净值 + IC 时序 + IC 直方图。

锁定 run_factor_grid_impl 三条产物契约：
1) quantile_nav 含 Q1-Q5 + LS 键，非空组起点归一化为 1.0
2) ic_series 长度 == dates 长度（逐期 IC）
3) ic_hist.bin_edges / counts 齐全，counts 之和 <= 有效 IC 样本数
"""
from unittest.mock import patch

import pandas as pd


def _fake_returns_panel():
    """构造 30 日 × 8 标的的日收益面板（足够算 20 日动量 + 5 分层）。"""
    import numpy as np
    dates = pd.bdate_range("2024-01-02", periods=30)
    syms = [f"S{i}.SZ" for i in range(8)]
    rng = np.random.default_rng(42)
    return pd.DataFrame(rng.normal(0.001, 0.02, (30, 8)), index=dates, columns=syms)


def test_factor_grid_returns_quantile_and_ic_payload():
    """run_factor_grid_impl 必须返 quantile_nav(Q1-Q5+LS) + ic_series + ic_hist。"""
    from server.celery_app import run_factor_grid_impl

    returns = _fake_returns_panel()

    with patch("data.lake_reader.DataLakeReader.get_instance") as MockReader:
        MockReader.return_value.loaded = True
        # reader.get_timeseries(sym, ...) → 该 sym 的累计 close DataFrame（含 close 列）
        MockReader.return_value.get_timeseries.side_effect = (
            lambda sym, s, e, **kw: (1 + returns[sym]).cumprod().rename("close").to_frame()
        )
        spec = {
            "factor": "cross_sectional_momentum",
            "universe": list(returns.columns),
            "start": "2024-01-02", "end": "2024-01-30",
        }
        out = run_factor_grid_impl(spec)

    assert out["ok"] is True
    # IC 时序
    assert isinstance(out["ic_series"], list)
    assert len(out["ic_series"]) == len(out["dates"])
    assert "ic_mean" in out and "ic_ir" in out and "t_stat" in out
    # 分层累计净值：Q1..Q5 + LS 键齐全
    qn = out["quantile_nav"]
    for key in ("Q1", "Q2", "Q3", "Q4", "Q5", "LS"):
        assert key in qn and isinstance(qn[key], list)
    # 非空组起点必须归一化为 1.0（随机数据下不强制每组都非空，但 Q1/Q5 应有数据）
    for key in ("Q1", "Q5"):
        if qn[key]:
            assert abs(qn[key][0] - 1.0) < 1e-9, f"{key} 起点必须归一化为 1.0"
    # IC 直方图
    assert "bin_edges" in out["ic_hist"] and "counts" in out["ic_hist"]
    assert sum(out["ic_hist"]["counts"]) <= len(out["ic_series"])


def test_factor_grid_empty_universe_returns_not_ok():
    """数据湖未加载（reader.loaded=False）→ {ok: False}，不抛。"""
    from server.celery_app import run_factor_grid_impl
    with patch("data.lake_reader.DataLakeReader.get_instance") as MockReader:
        MockReader.return_value.loaded = False  # 离线模式
        out = run_factor_grid_impl({
            "factor": "x", "universe": [], "start": "2024-01-02", "end": "2024-01-30",
        })
    assert out["ok"] is False
