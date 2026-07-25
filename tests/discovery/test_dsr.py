# -*- coding: utf-8 -*-
"""Deflated Sharpe Ratio 测试（闭式，纯 stdlib）。"""
import pytest


def test_dsr_decreases_with_more_trials():
    """多重比较修正：试越多，同 sharpe 的 DSR 越低（最高更可能是运气）。"""
    from discovery.dsr import deflated_sharpe
    s_few = deflated_sharpe(sharpe=2.0, n_trials=10, n_obs=100)
    s_many = deflated_sharpe(sharpe=2.0, n_trials=1000, n_obs=100)
    assert s_few > s_many


def test_dsr_increases_with_sharpe():
    """更高 sharpe → DSR 更高（更显著）。"""
    from discovery.dsr import deflated_sharpe
    low = deflated_sharpe(sharpe=1.0, n_trials=10, n_obs=100)
    high = deflated_sharpe(sharpe=3.0, n_trials=10, n_obs=100)
    assert high > low


def test_dsr_increases_with_observations():
    """更长样本 → DSR 更高（样本量越大越显著）。"""
    from discovery.dsr import deflated_sharpe
    short = deflated_sharpe(sharpe=2.0, n_trials=10, n_obs=30)
    long = deflated_sharpe(sharpe=2.0, n_trials=10, n_obs=500)
    assert long > short


def test_dsr_range():
    """DSR ∈ [0,1]（是概率）。"""
    from discovery.dsr import deflated_sharpe
    for sh in [-1.0, 0.0, 1.0, 5.0]:
        v = deflated_sharpe(sharpe=sh, n_trials=5, n_obs=100)
        assert 0.0 <= v <= 1.0


def test_dsr_single_trial_no_multiple_comparison():
    """n_trials=1 → 无多重比较，SR_max=0，DSR 仅看 sharpe 显著性。"""
    from discovery.dsr import deflated_sharpe
    v = deflated_sharpe(sharpe=2.0, n_trials=1, n_obs=100)
    assert 0.0 <= v <= 1.0


def test_norm_ppf_matches_scipy():
    """DSR 闭式的 Acklam 逆正态须与 scipy.stats.norm.ppf 数值一致（锁数学，防系数抄错）。

    物理意图：DSR 公式依赖 Φ^{-1}(p)，discovery/dsr.py 用 Acklam 算法纯 Python 实现（不引
    scipy 作运行时依赖）。Acklam 系数抄错一个数字 → DSR 系统性偏差且无报警。本测试用 scipy
    权威实现做 golden 对照，防系数抄错（抄错一个数字通常偏差 ≥1e-5，远大于 Acklam 算法误差）。

    阈值口径选择（绝对 abs < 5e-9，非相对）：取 Acklam 实测最大绝对误差 2.23e-9 × 2.2 浮点
    余量 = 5e-9。baseline 实测：p=0.02425/0.97575 处 abs err 2.23e-9、p=0.001/0.999 处 1.46e-9，
    均在 5e-9 内有 >2x 余量，GREEN 稳定。

    双面保证（诚实记录算法边界，非测试设计的美化）：
      - 高位抄错（影响结果量级的抄错，偏差 >>1e-8）：100% 捕获——这是本测试真正的价值。
      - 末位微扰（1e-10 及以下量级）：受 Acklam 算法固有噪声（~2e-9）遮蔽，不保证捕获；
        这是基于数值对照的算法边界，非缺陷。Acklam 1996 算法本就是"相对误差 < 1.15e-9"
        的有理逼近，对末位微扰不敏感是其数学性质，不能用测试阈值强行掩盖。
    """
    scipy_stats = pytest.importorskip("scipy.stats")   # CI 无 scipy 则 skip，非运行时依赖
    from discovery.dsr import _norm_ppf
    # 覆盖 Acklam 三段分支：下尾(p<0.02425) / 中段 / 上尾(p>1-0.02425)
    for p in [0.001, 0.01, 0.02425, 0.1, 0.5, 0.9, 0.97575, 0.99, 0.999]:
        sp = scipy_stats.norm.ppf(p)
        # 绝对口径：Acklam 实测 max abs err 2.23e-9 × 2.2 余量 = 5e-9
        # 高位抄错（偏差 >>1e-8）100% 捕获；末位微扰（1e-10-）受算法噪声限制
        assert abs(_norm_ppf(p) - sp) < 5e-9, f"p={p} 绝对偏差超 5e-9（Acklam 精度内）"
