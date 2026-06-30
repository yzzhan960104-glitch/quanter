"""单因子评估引擎：秩相关 IC（Rank IC）+ 分层收益测试。纯 Pandas，禁 Alphalens。

IC 用 factor.rank().corrwith(fwd.rank(), axis=1) 逐日横截面 Spearman —— 无需 scipy。
分层用 pd.qcut 逐日分组，聚合各组远期收益与多空价差。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


class FactorAnalyzer:
    """单因子评估：IC 与分层。"""

    def compute_ic(self, factor: pd.DataFrame, fwd_returns: pd.DataFrame) -> dict:
        """逐日横截面秩相关 IC。

        参数：factor / fwd_returns 均为 DataFrame(index=date, columns=symbol)。
        返回：{ic_series, ic_mean, ic_ir, t_stat}。

        Why Rank IC（Spearman）而非普通 Pearson IC：
        - 普通相关对极端值/非正态收益极度敏感，易被少数妖票主导，信号方向被扭曲；
        - 秩相关只看横截面排序，稳健于离群点，更贴合“高分组组优于低分”这一选股直觉；
        - 纯 pandas rank+corrwith 实现逐日横截面 Spearman，零依赖、无 scipy 黑盒。
        """
        # 仅保留因子与远期收益日期对齐的样本，杜绝错位/前视污染
        aligned = factor.index.intersection(fwd_returns.index)
        if len(aligned) == 0:
            # 空输入安全：无对齐日则返回中性结果，绝不向上抛异常
            return {"ic_series": pd.Series(dtype=float), "ic_mean": 0.0,
                    "ic_ir": 0.0, "t_stat": 0.0}
        f = factor.loc[aligned]
        r = fwd_returns.loc[aligned]
        # axis=1 表示逐行（即逐交易日横截面）求 Spearman 相关
        ic = f.rank().corrwith(r.rank(), axis=1).dropna()
        if ic.empty or ic.std() == 0:
            # ic.std()==0 视为无信息（或全 NaN 单点），返回 0.0 以免下游除零/NaN 扩散
            return {"ic_series": ic, "ic_mean": float(ic.mean() if not ic.empty else 0.0),
                    "ic_ir": 0.0, "t_stat": 0.0}
        return {
            "ic_series": ic,
            "ic_mean": float(ic.mean()),
            # ICIR = IC均值/IC标准差，衡量信号稳定性（单位波动下的预测力）
            "ic_ir": float(ic.mean() / ic.std()),
            # t 统计量 = ICIR * sqrt(N)，N 为有效横截面日数，检验 IC 显著性
            "t_stat": float(ic.mean() / ic.std() * np.sqrt(len(ic))),
        }

    def fractile_analysis(self, factor: pd.DataFrame, fwd_returns: pd.DataFrame,
                          n_groups: int = 5) -> dict:
        """逐日分层：pd.qcut 分 n_groups，聚合各组远期收益序列 + 多空价差。

        Why pd.qcut + duplicates="drop"：
        - qcut 默认等频分箱，但因子值常出现大量并列（如多只股票因子值相同），
          导致等频边界重叠、qcut 抛 ValueError；duplicates="drop" 合并重叠边界，
          容忍非唯一分位、保证逐日分箱稳健不抛。
        - Why 逐日而非全期 qcut：因子横截面分布在每个交易日都不同，
          全期分箱会引入跨日前视偏差（用未来横截面分布给当日分组），违背时序因果。
        """
        aligned = factor.index.intersection(fwd_returns.index)
        group_series = {g: [] for g in range(n_groups)}
        for dt in aligned:
            f = factor.loc[dt].dropna()
            r = fwd_returns.loc[dt].reindex(f.index).dropna()
            common = f.index.intersection(r.index)
            # 当日可用标的少于分组数则无法有效分箱，跳过避免空组
            if len(common) < n_groups:
                continue
            f, r = f.loc[common], r.loc[common]
            try:
                # labels=False 返回整数组号（0..n-1），duplicates="drop" 防重复边界
                bins = pd.qcut(f, n_groups, labels=False, duplicates="drop")
            except ValueError:
                # 极端退化（如全同值）即使去重也无法分箱，跳过当日
                continue
            for g in pd.unique(bins.dropna()):
                mask = bins == g
                if mask.any():
                    # 记录当日该组远期收益均值
                    group_series.setdefault(int(g), []).append(r[mask].mean())
        result = {g: pd.Series(v, dtype=float) for g, v in group_series.items()}
        # 多空 = 最高组 - 最低组（最高组因子值最大，应预示更高远期收益）
        max_g = max(result.keys()) if result else None
        min_g = min(result.keys()) if result else None
        if max_g is not None and min_g is not None:
            ls = result.get(max_g, pd.Series(dtype=float)) - \
                 result.get(min_g, pd.Series(dtype=float))
        else:
            ls = pd.Series(dtype=float)
        return {"group_returns": result, "long_short": ls}
