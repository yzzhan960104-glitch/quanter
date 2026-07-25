# -*- coding: utf-8 -*-
"""daily 迁移一致性：新 _sync_by_symbol adj_api 管道 vs 旧 sync_data_lake.fetch_qfq 公式等价（Plan Task 12）。

物理意图：保护既有 a_shares_daily.parquet（903万行，旧 fetch_qfq 生产）不被新管道破坏。
新管道（Task 3）与旧 fetch_qfq 用同一前复权公式 price_qfq = raw × adj / latest（latest=区间最新），
本测试用同一份 mock raw+adj 输入，验证两者产出相同 close（数学等价 → 落湖等价）。
"""
from unittest.mock import MagicMock
import pandas as pd
import pytest

import scripts.sync_data_lake as sdl


def test_旧fetch_qfq公式产出与新管道Task3一致(monkeypatch):
    """同输入 raw+adj，旧 fetch_qfq 公式产出 close=[5.0, 11.0]（与 Task 3 新管道断言一致）。

    公式（fetch_qfq:118-128）：latest_af = adj.iloc[-1]（区间最新）；close = raw × adj / latest。
    raw close=[10,11]，adj=[1,2]，latest=2 → [10×1/2, 11×2/2] = [5.0, 11.0]。
    Task 3 新管道 _sync_by_symbol adj_api 块用同公式，断言同 [5.0, 11.0]（见 test_sync_ohlcv_qfq）。
    两者数学等价 → 新管道迁移不破坏既有 a_shares_daily 口径。
    """
    ts_code = "000001.SZ"
    raw = pd.DataFrame({
        "ts_code": [ts_code] * 2, "trade_date": ["20250101", "20250102"],
        "open": [10.0, 11.0], "high": [10.5, 11.5],
        "low": [9.5, 10.5], "close": [10.0, 11.0],
        "vol": [1000.0, 1100.0], "amount": [10000.0, 11000.0],
    })
    adj = pd.DataFrame({
        "ts_code": [ts_code] * 2, "trade_date": ["20250101", "20250102"],
        "adj_factor": [1.0, 2.0],
    })
    # mock sdl._fetch_with_guard：daily→raw, adj_factor→adj（绕过限频/熔断/真实 API）
    def fake_guard(pro, api_name, **kw):
        return raw if api_name == "daily" else adj
    monkeypatch.setattr(sdl, "_fetch_with_guard", fake_guard)

    old_df = sdl.fetch_qfq(MagicMock(), ts_code, "2025-01-01", "2025-01-02")
    # 旧管道前复权 close = [5.0, 11.0]（与 Task 3 新管道 test_adj_api_触发前复权重建 断言完全一致）
    assert old_df["close"].tolist() == pytest.approx([5.0, 11.0], rel=1e-6), \
        "旧 fetch_qfq 前复权 close 应为 [5.0, 11.0]（与新管道 Task3 一致）"
    # volume 不复权（旧管道同样不复权）
    assert old_df["volume"].tolist() == pytest.approx([1000.0, 1100.0], rel=1e-6)


def test_fetch_qfq函数保留可用():
    """sync_data_lake.fetch_qfq 函数保留（__main__ 转薄壳，但函数体保留供一致性对比 + 回归）。"""
    assert callable(sdl.fetch_qfq), "fetch_qfq 函数应保留（test_daily_migration_parity + 旧管道回归用）"
