# -*- coding: utf-8 -*-
"""次日计划预演（2026-09-01 桥第三块）：纯 helper 单测。

build_preview 依赖湖+实盘 state+7002（集成面由 09-01 实跑验证），此处只锁
amihud_from_frame 的口径与门槛语义（与产物 fetch_amihud60 同公式）。
"""
from __future__ import annotations

import pandas as pd

from ops.emquant_plan_preview import amihud_from_frame


def _frame(n: int) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"close": [10.0 + 0.1 * i for i in range(n)],
         "amount": [1_000.0] * n}, index=idx)


def test_amihud_positive_and_gated():
    df = _frame(62)
    v = amihud_from_frame(df, window=60, min_days=48)
    assert v is not None and v > 0
    # 有效根数 < min_days → None（fail-open 语义由 apply_amihud_filter 豁免承担）
    assert amihud_from_frame(df, window=60, min_days=62) is None
    # 零成交额行不计入有效根
    df0 = df.copy()
    df0.loc[df0.index[:40], "amount"] = 0.0
    assert amihud_from_frame(df0, window=60, min_days=48) is None
    # 异常（缺列）安静返 None 不抛
    assert amihud_from_frame(df.drop(columns=["amount"]), 60, 48) is None
