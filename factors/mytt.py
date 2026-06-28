"""MyTT —— 通达信/同花顺指标库的 Python 纯向量化实现

设计哲学（对齐 CLAUDE.md 反黑盒、第一性原理）：
- 不引入第三方 mytt 包，自行用 pandas/numpy 逐函数翻译通达信公式
- 所有函数输入输出均为 pd.Series，索引即时间轴，天然对齐 OHLCV
- 纯向量化（rolling / ewm 算子），禁用 for 循环逐点计算

通达信约定（与同花顺一致，区别于部分西方指标库）：
- EMA 用 adjust=False 的递归式：y_0 = x_0; y_t = α·x_t + (1-α)·y_{t-1}, α = 2/(n+1)
- BOLL 用总体标准差 ddof=0
- MACD 柱状图 hist = (DIF - DEA) * 2

前视偏差说明：
本模块是纯数学计算，不涉及时间位移。前视偏差由调用方（策略层）用 shift(1) 控制，
与现有 factors/technical.py 的处理方式一致。
"""
import pandas as pd


def EMA(s: pd.Series, n: int) -> pd.Series:
    """
    指数移动平均（通达信递归式）

    物理含义：对近期数据赋予指数衰减的更高权重，比简单移动平均对价格变化更敏感、更平滑。
    通达信约定 adjust=False：首个值直接取输入首值，后续按递归式展开，
    避免adjust=True 在序列起始处的"归一化偏移"。

    参数：
        s: 输入序列（通常为 close）
        n: 计算周期（span）

    返回：
        与 s 同索引的 EMA 序列（全程无 NaN）
    """
    return s.ewm(span=n, adjust=False).mean()


def MA(s: pd.Series, n: int) -> pd.Series:
    """
    简单移动平均（Simple Moving Average）

    物理含义：过去 n 期的算术平均，最基础的均线。
    前 n-1 个值为 NaN（窗口未满），由调用方决定如何填充（ffill 或丢弃）。

    参数：
        s: 输入序列
        n: 计算周期

    返回：
        与 s 同索引的 MA 序列（前 n-1 个为 NaN）
    """
    return s.rolling(window=n).mean()
