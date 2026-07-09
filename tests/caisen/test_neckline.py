# -*- coding: utf-8 -*-
"""颈线线性回归测试。

覆盖三种颈线形态：
- 水平颈线（两峰等高）：任一点颈线价 = 峰价，斜率 0；
- 上倾颈线（右峰更高）：t 处颈线价为两端线性插值；
- 下倾颈线（右峰更低）：斜率为负。

回归基元用 numpy polyfit（degree=1）显式实现，便于审计与多点扩展。
"""
import pytest
from caisen.patterns import neckline


def test_horizontal_neckline():
    """水平颈线：两峰等高 → 任一点颈线价 = 峰价。"""
    p1, p2 = (0, 10.0), (10, 10.0)
    assert neckline.neckline_at(5, *p1[:0], *p1, *p2) == pytest.approx(10.0)


def test_rising_neckline():
    """上倾颈线：点1(0,10)、点2(10,12) → t=5 处颈线=11。"""
    val = neckline.fit_line([(0, 10.0), (10, 12.0)], at=5)
    assert val == pytest.approx(11.0)


def test_declining_slope_negative():
    """下倾颈线斜率为负。"""
    assert neckline.slope((0, 12.0), (10, 10.0)) < 0
