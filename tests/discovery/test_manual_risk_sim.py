# -*- coding: utf-8 -*-
"""manual_risk_sim 单测（R3 · D0）：滞回状态机 / 边界 / 防御 / 7 月形态回归。

import 说明：不加 sys.path.insert——tests/__init__.py + tests/discovery/__init__.py
的包链让 pytest 把仓库根入 path；手工插 tests/ 反而会把 `import discovery` 遮蔽
解析到 tests/discovery/（本文件 D0 实测踩过）。
"""
import pandas as pd
import pytest
from datetime import date

from discovery.manual_risk_sim import (ManualRiskRule, build_block_calendar,
                                       is_blocked, _pool_equity)


def _mk_universe(closes: dict[str, list[float]], start="2026-01-01"):
    """合成 universe：{symbol: 收盘序列} → freeze 同构 dict[symbol→DataFrame]。"""
    idx = pd.bdate_range(start, periods=len(next(iter(closes.values()))))
    return {s: pd.DataFrame({"close": pd.Series(v, index=idx),
                             "open": 1.0, "high": 1.0, "low": 1.0,
                             "volume": 1.0, "amount": 1.0}, index=idx)
            for s, v in closes.items()}


class TestRule:
    def test_滞回几何校验(self):
        with pytest.raises(ValueError):
            ManualRiskRule(trigger=-0.10, release=-0.15)   # trigger 须更深
        with pytest.raises(ValueError):
            ManualRiskRule(release=0.05)                   # release 须为负
        with pytest.raises(ValueError):
            ManualRiskRule(window=1)                       # 窗宽下限


class TestStateMachine:
    def test_深回撤触发并滞回保持(self):
        # 60 个交易日：前 20 平（基准窗），再 10 日跌到 −20%（池子回撤），后 5 日
        # 反弹到 −13%（仍在防抖带内），再 5 日修复到 −5%（解除），再 20 平。
        leg = [1.0] * 20 + [1 - 0.02 * i for i in range(1, 11)] \
            + [0.80 + 0.014 * i for i in range(1, 11)] + [1.0] * 20
        uni = _mk_universe({"A": leg, "B": leg})
        cal = build_block_calendar(uni, ManualRiskRule(window=20, trigger=-0.15, release=-0.10))
        assert cal, "−20% 回撤必须触发"
        days = sorted(cal)
        # 解除后不得再拦（尾部 20 日全平，回撤已修复）
        idx = pd.bdate_range("2026-01-01", periods=len(leg))
        assert idx[-1].date() not in cal
        # 防抖带内（−13%）保持拦截：触发后第 11-15 日（反弹到 −13% 段）仍在日历
        assert any(idx[30 + i].date() in cal for i in range(5))

    def test_浅回撤不触发(self):
        leg = [1.0] * 20 + [1 - 0.014 * i for i in range(1, 8)] + [1.0] * 20   # 最深 −9.8%
        uni = _mk_universe({"A": leg, "B": leg})
        assert build_block_calendar(uni, ManualRiskRule()) == frozenset()

    def test_边界恰触(self):
        # 恰好 −15% 边界日：dd ≤ trigger 触发（含等）
        leg = [1.0] * 20 + [1 - 0.015 * i for i in range(1, 11)] + [1.0] * 20   # 最深 −15%
        uni = _mk_universe({"A": leg, "B": leg})
        assert build_block_calendar(uni, ManualRiskRule()) != frozenset()


class TestDefensive:
    def test_数据短于窗返空(self):
        uni = _mk_universe({"A": [1.0] * 10, "B": [1.0] * 10})
        assert build_block_calendar(uni, ManualRiskRule(window=20)) == frozenset()

    def test_空close列炸干净(self):
        with pytest.raises(ValueError):
            _pool_equity({"A": pd.DataFrame({"open": [1.0]})})

    def test_停牌股跳过(self):
        # B 全程 NaN 收益（停牌）——等权只吃 A，行为不炸
        leg = [1.0] * 20 + [1 - 0.03 * i for i in range(1, 8)] + [1.0] * 32
        idx = pd.bdate_range("2026-01-01", periods=len(leg))
        uni = {"A": pd.DataFrame({"close": pd.Series(leg, index=idx)}),
               "B": pd.DataFrame({"close": pd.Series([None] * len(leg), index=idx)})}
        cal = build_block_calendar(uni, ManualRiskRule())
        assert cal  # A 的 −21% 回撤仍触发


class TestConsumption:
    def test_is_blocked_类型容忍(self):
        cal = frozenset({date(2026, 7, 17)})
        assert is_blocked(pd.Timestamp("2026-07-17"), cal)
        assert is_blocked(date(2026, 7, 17), cal)
        assert not is_blocked(pd.Timestamp("2026-07-16"), cal)

    def test_起止裁剪(self):
        leg = [1.0] * 20 + [1 - 0.03 * i for i in range(1, 8)] + [1.0] * 32
        uni = _mk_universe({"A": leg, "B": leg})
        full = build_block_calendar(uni)
        clipped = build_block_calendar(uni, start="2026-02-01")
        assert clipped <= full
