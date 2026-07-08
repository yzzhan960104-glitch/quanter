# -*- coding: utf-8 -*-
"""RiskManager 测试：宏观系数三态、HV 过滤、流动性、仓位 5% 钳制。"""
import numpy as np
import pandas as pd
import pytest

from caisen.config import StrategyConfig
from caisen.risk import RiskManager


def test_macro_coef_three_states(monkeypatch):
    """regime +1→1.0, 0→0.6, -1→0.0。"""
    rm = RiskManager(StrategyConfig())
    class FakeRegime:
        def compute(self, d): return d  # 透传：用日期值模拟 regime
    rm.regime = FakeRegime()
    assert rm.macro_position_coef(1) == pytest.approx(1.0)
    assert rm.macro_position_coef(0) == pytest.approx(0.6)
    assert rm.macro_position_coef(-1) == pytest.approx(0.0)

def test_liquidity_filter():
    """近30日均成交额 ≥ 1亿 通过。"""
    rm = RiskManager(StrategyConfig())
    idx = pd.RangeIndex(40)
    df = pd.DataFrame({"amount": [2e8]*40}, index=idx)
    assert rm.liquidity_filter(df.tail(30)) is True
    df_low = pd.DataFrame({"amount": [5e7]*40}, index=idx)
    assert rm.liquidity_filter(df_low.tail(30)) is False

def test_position_size_capped_at_5pct():
    """仓位被 max_position_pct 5% 硬钳。"""
    rm = RiskManager(StrategyConfig())
    shares = rm.position_size(aum=1_000_000, entry=10.0, stop=9.0, coef=1.0)
    # 5% 上限 = 50000 元 / entry 10 → 5000 股，向下取整到 100 整手
    assert shares <= 5000
    assert shares % 100 == 0


# ---------------------------------------------------------------------------
# micro_filter 单测（Task 5 Minor follow-up，归 Task 8 补）
# 物理意图：近 hv_window(20) 的 HV 分位 > hv_max_quantile(0.95) → 剔除；
#           样本不足 (< hv_window) 或 HV 全 NaN → 放行（保守不误杀）。
# ---------------------------------------------------------------------------
def test_micro_filter_excludes_high_hv():
    """HV 异常标的（末值 HV > 窗口 95 分位）被 micro_filter 判否。

    构造：前 19 根平稳（HV 低）+ 末 20 根注入单日 ±15% 剧烈跳变（HV 飙升）。
    末根 HV 处于近 20 日样本的极端高位 → > 0.95 分位 → 剔除。
    """
    rm = RiskManager(StrategyConfig())   # hv_window=20, hv_max_quantile=0.95
    np.random.seed(7)
    n = 40
    # 前段：平稳价格（HV 低）
    close_pre = np.cumsum(np.random.normal(0, 0.01, 20)) + 10.0
    # 后段：剧烈震荡（HV 飙升）—— 单日 ±15% 跳变
    close_post = [close_pre[-1]]
    for _ in range(20):
        close_post.append(close_post[-1] * (1 + np.random.choice([-1, 1]) * 0.15))
    close = np.concatenate([close_pre, np.array(close_post[1:])])
    df = pd.DataFrame({"close": close})
    ok, reason = rm.micro_filter(df, "HIGH_HV")
    assert ok is False, f"HV 异常标的应被剔除，但放行（reason={reason}）"


def test_micro_filter_passthrough_when_insufficient_samples():
    """样本不足（< hv_window）时放行——小样本分位无统计意义，不误杀新股。

    构造：仅 5 根价格（远 < hv_window=20），micro_filter 应放行。
    """
    rm = RiskManager(StrategyConfig())   # hv_window=20
    df = pd.DataFrame({"close": [10.0, 11.0, 9.5, 10.5, 9.8]})   # 仅 5 根
    ok, reason = rm.micro_filter(df, "NEW_STOCK")
    assert ok is True, f"样本不足应放行（不误杀新股），但被剔除（reason={reason}）"


def test_micro_filter_passthrough_normal_hv():
    """正常 HV 标的（末值 HV 未超 95 分位）放行——对照用例，证明过滤是有选择性的。"""
    rm = RiskManager(StrategyConfig())   # hv_window=20, hv_max_quantile=0.95
    np.random.seed(11)
    # 平稳随机游走（HV 稳定，末值不会处于 95 分位）
    close = np.cumsum(np.random.normal(0, 0.01, 60)) + 10.0
    df = pd.DataFrame({"close": close})
    ok, reason = rm.micro_filter(df, "NORMAL")
    assert ok is True, f"正常 HV 标的应放行，但被剔除（reason={reason}）"
