# -*- coding: utf-8 -*-
"""颈线：两点的线性回归（蔡森形态支撑/压力线基元）。

物理意图：颈线是 W 底 / 头肩底等形态的关键水准——
- W 底：两低点连线即为颈线（支撑），价格收盘突破颈线即形态确认；
- 头肩底：两肩低点连线为颈线（压力），突破后量度涨幅 = 颈线价 + |头底-颈线|。

实现选择：显式 numpy polyfit（degree=1 一阶线性回归），不引入 trendln 黑盒。
trendln 可在 screener 层做交叉校验（可选），核心回归用 polyfit 保证可审计：
- 两点输入时 polyfit 退化为两点连线（解析解）；
- 多点输入时为最小二乘回归（多点颈线更稳，可平滑噪声极值点）。

风控边界：
- 同一 idx 的两点（p2[0]==p1[0]）slope 返回 0.0，避免除零（垂直线无经济意义）；
- polyfit 对 xs 全相等会触发 RankWarning 并返回 NaN，调用方需保证至少两个不同 idx。
"""
from __future__ import annotations
import numpy as np


def slope(p1: tuple, p2: tuple) -> float:
    """两点斜率 (p2.price-p1.price)/(p2.idx-p1.idx)。

    p1/p2 形如 (idx, price)。idx 相同时返回 0.0（防除零，垂直线无颈线意义）。
    """
    return (p2[1] - p1[1]) / (p2[0] - p1[0]) if p2[0] != p1[0] else 0.0


def fit_line(points: list[tuple], at: int) -> float:
    """对 points=[(idx, price), ...] 做一阶多项式回归，返回 x=at 处的 y。

    两点时等价于两点连线；多点时为最小二乘回归（多点颈线更稳）。
    用 numpy polyfit degree=1：返回系数 [k, b]，即 y = k*x + b。
    """
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    k, b = np.polyfit(xs, ys, 1)   # 一阶：y = k*x + b
    return float(k * at + b)


def neckline_at(t: int, *pts) -> float:
    """颈线在 t 处的价（兼容两点直传）。

    pts 形如 (idx1, price1, idx2, price2, ...)，按 (idx, price) 两两分组。
    内部统一走 fit_line：两点连线 / 多点最小二乘。
    """
    points = [(pts[i], pts[i + 1]) for i in range(0, len(pts), 2)]
    return fit_line(points, at=t)
