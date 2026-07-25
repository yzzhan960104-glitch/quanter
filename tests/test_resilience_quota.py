# -*- coding: utf-8 -*-
"""双桶限频测试：基础500/特色300 + 别名向后兼容（Plan Task 1）。

物理意图：账户升级后 Tushare 官方配额分两档（基础500/分、特色300/分），
原单桶 ~20/分（capacity=3, refill_rate=0.33）是已废弃 tnskhdata 代理的旧参数。
本测试钉死双桶独立实例 + 别名向后兼容（4 处旧调用零改）。
"""
from data.resilience import (
    tushare_rate_limiter_basic,
    tushare_rate_limiter_special,
    tushare_rate_limiter,
)


def test_双桶独立实例():
    """基础桶与特色桶是两个独立 RateLimiter 实例（互不干扰令牌计数）。"""
    assert tushare_rate_limiter_basic is not tushare_rate_limiter_special
    assert tushare_rate_limiter_basic.name == "tushare_basic"
    assert tushare_rate_limiter_special.name == "tushare_special"


def test_别名指向基础桶():
    """旧名 tushare_rate_limiter 必须是 basic 别名（fetcher/sync 等 4 处旧调用零改）。"""
    assert tushare_rate_limiter is tushare_rate_limiter_basic


def test_基础桶配额_约500每分():
    """refill_rate × 60 ≈ 500/min（留 ~1% 余量避免边界抖动触发 429）。"""
    # refill_rate 单位 token/s，× 60 = 每分补充量 ≈ 官方配额
    assert 490 <= tushare_rate_limiter_basic.refill_rate * 60 <= 500
    # capacity 给少量突发（官方滑动窗口边界允许短时突发）
    assert tushare_rate_limiter_basic.capacity >= 5


def test_特色桶配额_约300每分():
    """refill_rate × 60 ≈ 300/min。"""
    assert 290 <= tushare_rate_limiter_special.refill_rate * 60 <= 300
    assert tushare_rate_limiter_special.capacity >= 3
