# -*- coding: utf-8 -*-
"""PatternScreener 编排器（蔡森形态学流水线 Phase 2 · Task 8）。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本模块是蔡森形态学流水线的"指挥官"——对标的池逐个串行执行：
        流动性过滤 → 微观波动率过滤(micro_filter) → 因果 ZigZag(causal_pivots + ATR)
        → W底/头肩底识别 → 命中收集 → 按近 30 日成交额降序输出候选 DataFrame。
    所有阈值/识别逻辑全部委托给既有组件（RiskManager / causal_pivots / w_bottom /
    head_shoulder），本模块只负责"串接 + 收集 + 排序"，不引入新的识别规则——
    这是显式至上原则的体现：编排逻辑扁平可审计，每一步都能追溯到既有组件。

编排链路（每步硬否决，任一失败即跳过该 symbol）：
    1. 流动性过滤：近 30 日均成交额 < liquidity_min_amount → 跳过
       （流动性不足的标的不参与形态交易，避免突破后无量拉升→闪崩）；
    2. micro_filter：近 hv_window 的 HV 分位 > hv_max_quantile → 跳过
       （HV 异常标的处于无序暴动，颈线突破统计有效性大幅下降）；
    3. causal_pivots + ATR：因果 ZigZag 提取 pivot（未来函数隔离，T 日看 T-1 及之前）；
    4. w_bottom.detect / head_shoulder.detect：
       - W 底用 cfg.max_pattern_depth（默认 0.30，W 底颈线高度比天然 < 30%）；
       - 头肩底用 cfg.hs_max_pattern_depth（默认 1.0，Task 7 follow-up：头部幅度天然更深）；
    5. 命中收集：任一形态 is_valid=True 即收集为候选，pattern_type 标记具体形态；
       两个形态都命中时取颈线满足空间更大者（depth 更大 = 头部更深 = 量度涨幅更大）；
    6. 排序：按近 30 日成交额(amount30d)降序，优先输出流动性最好的候选。

输出 DataFrame 字段物理意图：
    symbol：标的代码；
    pattern_type：形态类型 ∈ {"w_bottom", "head_shoulder"}；
    formed_at：形态形成日（pivot 末点日期，用 DataFrame index 末值）；
    breakout_price：颈线突破价（W底=P4 收盘价 / 头肩底=P7 收盘价）；
    neckline_price：颈线价（形态识别组件返回的 neckline_price）；
    depth：颈线高度比（形态幅度，用于双形态择优）；
    tension：幅宽张力（高度/宽度，张力越强交易价值越高）；
    amount30d：近 30 日均成交额（排序键，流动性度量）；
    is_valid：形态综合判定是否有效（恒为 True，因为仅收集命中候选）。

风控边界（CLAUDE.md 极简 + 量化风控拷问）：
    - 任一组件异常不中断整个扫描：单个 symbol 抛异常时记录 None 并跳过，保证
      标的池扫描完整性（避免一只标的的数据脏值拖垮全市场扫描）；
    - micro_filter/流动性过滤在形态识别前执行，前置剔除无序/冷门标的，既加速也防
      在无意义标的上浪费识别算力；
    - causal_pivots 已保证 pivot 因果无未来函数，本模块纯前向消费 pivot 序列。
"""
from __future__ import annotations

import pandas as pd

from caisen.config import StrategyConfig
from caisen.risk import RiskManager
from caisen.patterns.zigzag_causal import causal_pivots, compute_atr
from caisen.patterns.w_bottom import detect as w_detect
from caisen.patterns.head_shoulder import detect as hs_detect


class PatternScreener:
    """蔡森形态学编排器（无状态计算，线程安全）。

    构造时绑定 StrategyConfig + RiskManager，screen() 对每个 symbol 串行执行
    过滤→识别→收集，返回按 amount30d 降序的候选 DataFrame。
    """

    def __init__(self, cfg: StrategyConfig, risk: RiskManager):
        """初始化编排器。

        参数：
            cfg:  蔡森策略全参数模型（形态识别阈值/流动性/HV/hs_max_pattern_depth 等
                  的真相源）。
            risk: 事前风控管理器（提供 liquidity_filter / micro_filter，已由 Task 5 实现）。

        设计意图（显式至上）：
            不在内部 new RiskManager——由调用方注入便于测试替身与单一配置源；cfg 与 risk
            的 cfg 应为同一实例（否则阈值漂移），由调用方负责保证。
        """
        self.cfg = cfg
        self.risk = risk

    def screen(self, price_data: dict, date) -> pd.DataFrame:
        """对标的池逐个执行过滤→识别→收集，按 amount30d 降序返回候选 DataFrame。

        参数：
            price_data: {symbol: DataFrame} 字典。每个 DataFrame 至少含列
                close / high / low / volume / amount（index 为交易日，可为整数或日期）。
            date: 当前交易日（保留给宏观 regime 判定/回测对齐，本编排未直接使用——
                  流动性/HV/pivots 全部基于 price_data 内部时序，无前视）。

        返回：
            DataFrame[symbol, pattern_type, formed_at, breakout_price, neckline_price,
                      depth, tension, amount30d, is_valid]，按 amount30d 降序。
            无命中时返回空 DataFrame（含完整列名，便于下游 concat/字段访问）。

        编排流程（每个 symbol）：
            1. 流动性过滤（risk.liquidity_filter）：不过 → 跳过；
            2. micro_filter（risk.micro_filter）：不过 → 跳过；
            3. causal_pivots + compute_atr：因果 pivot 提取（异常 → 跳过该 symbol）；
            4. w_bottom.detect（用 cfg.max_pattern_depth）：命中 → 收集；
            5. head_shoulder.detect（用临时宽 cfg：max_pattern_depth=hs_max_pattern_depth）：
               命中 → 收集；
            6. 双形态都命中 → 取 depth 更大者（颈线满足空间更大）；
            7. 全部 symbol 处理完 → 按 amount30d 降序输出。
        """
        candidates: list[dict] = []

        for symbol, df in price_data.items():
            # 防御性：单个 symbol 异常不拖垮全市场扫描（CLAUDE.md 量化风控·边界审查）
            try:
                hit = self._screen_one(symbol, df)
            except Exception:
                # 数据脏值/列缺失等异常 → 记 None 跳过，保证标的池完整性
                hit = None
            if hit is not None:
                candidates.append(hit)

        # —— 排序：按近 30 日成交额降序（流动性最好的候选优先）——
        if not candidates:
            # 无命中：返回含完整列名的空 DataFrame（下游 concat/字段访问不报错）
            return pd.DataFrame(columns=[
                "symbol", "pattern_type", "formed_at", "breakout_price",
                "neckline_price", "depth", "tension", "amount30d", "is_valid",
            ])
        out = pd.DataFrame(candidates)
        out = out.sort_values("amount30d", ascending=False, kind="mergesort").reset_index(drop=True)
        return out

    def _screen_one(self, symbol: str, df: pd.DataFrame) -> dict | None:
        """对单个 symbol 执行完整编排链路，返回候选 dict 或 None（未命中/被过滤）。

        本方法是 screen 的内循环主体，独立出来便于单 symbol 调试与异常隔离。
        所有过滤/识别组件的调用顺序与 screen 文档一致。
        """
        # —— 0. 列完整性兜底 ——
        # 缺列是数据脏值，screen 外层 try/except 会捕获，这里显式给出清晰错误信息。
        for col in ("close", "high", "low", "volume", "amount"):
            if col not in df.columns:
                raise KeyError(f"{symbol} 缺少必要列 {col}（需 close/high/low/volume/amount）")

        # —— 1. 流动性过滤：近 30 日均成交额 ≥ liquidity_min_amount ——
        # 物理意图：形态交易依赖颈线突破后的流动性承接，冷门标的突破后易闪崩。
        if not self.risk.liquidity_filter(df):
            return None

        # —— 2. micro_filter：近 hv_window HV 分位 > hv_max_quantile → 剔除 ——
        # 物理意图：HV 异常标的处于无序暴动，颈线突破统计有效性大幅下降。
        ok, _reason = self.risk.micro_filter(df, symbol)
        if not ok:
            return None

        # —— 3. causal_pivots + ATR：因果 ZigZag pivot 提取 ——
        # compute_atr 用 high/low/close 三序列，causal_pivots 基于 close + atr 提取 pivot。
        # 末尾 confirm_bars 内的 pivot 被保守丢弃（未来函数隔离，详见 zigzag_causal.py）。
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]
        atr = compute_atr(high, low, close)
        pivots = causal_pivots(close, atr, self.cfg)

        # —— 4. W 底识别（用 cfg.max_pattern_depth，W 底颈线高度比天然 < 30%）——
        w_res = w_detect(close, pivots, high, low, volume, self.cfg)

        # —— 5. 头肩底识别（用临时宽 cfg：max_pattern_depth=hs_max_pattern_depth）——
        # 物理意图（Task 7 follow-up concern 2）：头肩底头部幅度天然深于 W底颈线
        # （头底是区间最低、两肩之上），用 W 底的 0.30 默认阈值会误否决合法头肩底。
        # model_copy(update={...}) 浅拷贝 cfg 仅替换 max_pattern_depth，其他参数不变。
        hs_cfg = self.cfg.model_copy(update={"max_pattern_depth": self.cfg.hs_max_pattern_depth})
        hs_res = hs_detect(close, pivots, high, low, volume, hs_cfg)

        # —— 6. 命中收集：任一形态 is_valid=True 即收集；双命中取 depth 更大者 ——
        # depth 物理意图：颈线高度比 = 形态垂直幅度。depth 越大 → 颈线满足空间（量度涨幅）
        # 越大 → 交易价值越高。双形态命中时取 depth 更大者，等价于"颈线满足空间更大者"。
        hits: list[tuple[str, object]] = []
        if w_res is not None and w_res.is_valid:
            hits.append(("w_bottom", w_res))
        if hs_res is not None and hs_res.is_valid:
            hits.append(("head_shoulder", hs_res))

        if not hits:
            return None   # 两形态均未命中，跳过

        # 双形态命中：取 depth 更大者（颈线满足空间更大）
        pattern_type, res = max(hits, key=lambda h: h[1].depth)

        # —— amount30d：近 30 日均成交额（排序键，与流动性过滤同源数据）——
        amount30d = float(df["amount"].tail(30).mean())

        # —— formed_at：形态形成日 = DataFrame index 末值（pivot 末点的交易日）——
        # 用 index 末值而非 pivot 末点 idx，因为 causal_pivots 末尾 confirm_bars 内的 pivot
        # 被丢弃，但形态的"当前形成时点"就是数据末根（T 日收盘看 T-1 及之前，合法）。
        formed_at = df.index[-1]

        # —— breakout_price：颈线突破价 ——
        # W 底 P4 与头肩底 P7 均为"末尾已确认的突破 pivot"，但其 idx 可能不等于末根
        # （causal_pivots 末尾 confirm_bars 内 pivot 被丢弃，真实突破点落在末根之前）。
        # 这里统一用 close.iloc[-1] 作为"当前突破状态"的代表价——下游 plan.py 计算颈线
        # 满足时会用 res.neckline_price + depth×H 重新精算，breakout_price 仅作排序展示。
        breakout_price = float(close.iloc[-1])

        return {
            "symbol": symbol,
            "pattern_type": pattern_type,
            "formed_at": formed_at,
            "breakout_price": breakout_price,
            "neckline_price": float(res.neckline_price),
            "depth": float(res.depth),
            "tension": float(res.tension),
            "amount30d": amount30d,
            "is_valid": True,
        }
