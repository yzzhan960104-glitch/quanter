# -*- coding: utf-8 -*-
"""PatternScreener 编排器（蔡森形态学流水线 Phase 2 · Task 8）。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本模块是蔡森形态学流水线的"指挥官"——对标的池逐个串行执行：
        流动性过滤 → 微观波动率过滤(micro_filter) → 因果 ZigZag(causal_pivots + ATR)
        → 遍历形态注册表(PATTERNS)识别 → 命中收集 → 按近 30 日成交额降序输出候选 DataFrame。
    所有阈值/识别逻辑全部委托给既有组件（RiskManager / causal_pivots + 各形态 detect），
    本模块只负责"串接 + 收集 + 排序"，不引入新的识别规则——形态经 caisen.patterns.registry
    的 PATTERNS 显式注册表声明式接入（方案B，新形态只改 registry 加一行，本模块零改），
    编排逻辑扁平可审计，每一步都能追溯到既有组件。

编排链路（每步硬否决，任一失败即跳过该 symbol）：
    1. 流动性过滤：近 30 日均成交额 < liquidity_min_amount → 跳过
       （流动性不足的标的不参与形态交易，避免突破后无量拉升→闪崩）；
    2. micro_filter：近 hv_window 的 HV 分位 > hv_max_quantile → 跳过
       （HV 异常标的处于无序暴动，颈线突破统计有效性大幅下降）；
    3. causal_pivots + ATR：因果 ZigZag 提取 pivot（未来函数隔离，T 日看 T-1 及之前）；
    4. 遍历形态注册表 PATTERNS（caisen.patterns.registry）：对每个 PatternMeta 做
       enable 开关过滤 → depth 覆写(model_copy 替换 max_pattern_depth，如头肩底用
       hs_max_pattern_depth、三角形用 triangle_max_pattern_depth，头部/边长幅度天然
       深于 W底颈线高度比，需分类型宽阈值) → meta.detect 识别；单形态异常 try/except
       隔离（一个形态抛错只跳过该形态，不影响同 symbol 其他形态）；
    5. 命中收集：任一形态 is_valid=True 即收集为候选，pattern_type=meta.name 标记具体形态；
       多形态命中时取颈线满足空间更大者（depth 更大 = 头部更深 = 量度涨幅更大）；
    6. 排序：按近 30 日成交额(amount30d)降序，优先输出流动性最好的候选。

输出 DataFrame 字段物理意图：
    symbol：标的代码；
    pattern_type：形态类型 ∈ {"w_bottom", "head_shoulder", "triangle_bottom"}；
    formed_at：形态形成日（pivot 末点日期，用 DataFrame index 末值）；
    breakout_price：颈线突破价（粗算：统一用 close.iloc[-1]——P4/P7 的 idx 可能
                    因 causal_pivots 末尾 confirm_bars 丢弃而早于末根，故用末根
                    收盘价代表"当前突破状态"。精算颈线满足目标价留给 Task 9 plan.py，
                    此字段仅作排序展示）；
    neckline_price：颈线价（形态识别组件返回的 neckline_price）；
    depth：颈线高度比（形态幅度，用于多形态择优）；
    tension：幅宽张力（高度/宽度，张力越强交易价值越高）；
    amount30d：近 30 日均成交额（排序键，流动性度量）；
    pattern_height：仅 triangle_bottom 候选输出（三角形边长 P1−P2，供 plan.py 满足点用）；
    is_valid：形态综合判定是否有效（恒为 True，因为仅收集命中候选）。

风控边界（CLAUDE.md 极简 + 量化风控拷问）：
    - 任一组件异常不中断整个扫描：双层隔离——内层单形态 detect 异常 try/except 跳过
      该形态（debug 日志记 meta.name + symbol），外层单 symbol 异常记录 None 跳过整个
      标的，保证标的池扫描完整性（避免一只标的/一个形态的数据脏值拖垮全市场扫描）；
    - micro_filter/流动性过滤在形态识别前执行，前置剔除无序/冷门标的，既加速也防
      在无意义标的上浪费识别算力；
    - causal_pivots 已保证 pivot 因果无未来函数，本模块纯前向消费 pivot 序列。
"""
from __future__ import annotations

import logging

import pandas as pd

from caisen.config import StrategyConfig
from caisen.risk import RiskManager
from caisen.patterns.zigzag_causal import causal_pivots, compute_atr
from caisen.patterns.registry import PATTERNS, PatternMeta


# 模块级 logger：单 symbol 异常走 debug 级（不污染 prod 日志，但可调试追溯）
_logger = logging.getLogger(__name__)


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
            date: 当前交易日（预留字段，当前未用——Task 9 macro regime 接入后用于
                  宏观周期判定/回测对齐。本编排的流动性/HV/pivots 全部基于 price_data
                  内部时序，无前视，故 date 暂不参与计算）。

        返回：
            DataFrame[symbol, pattern_type, formed_at, breakout_price, neckline_price,
                      depth, tension, amount30d, is_valid]，按 amount30d 降序。
            无命中时返回空 DataFrame（含完整列名，便于下游 concat/字段访问）。

        编排流程（每个 symbol）：
            1. 流动性过滤（risk.liquidity_filter）：不过 → 跳过；
            2. micro_filter（risk.micro_filter）：不过 → 跳过；
            3. causal_pivots + compute_atr：因果 pivot 提取（异常 → 跳过该 symbol）；
            4. 遍历 PATTERNS 注册表（w_bottom/head_shoulder/triangle_bottom 等）：
               - enable_field 开关过滤（如 enable_triangle_bottom）；
               - depth_override_field 覆写 max_pattern_depth（hs/tri 分类型宽阈值）；
               - meta.detect 命中（is_valid=True）→ 收集；单形态异常隔离；
            5. 多形态命中 → 取 depth 更大者（颈线满足空间更大）；
            6. 全部 symbol 处理完 → 按 amount30d 降序输出。
        """
        candidates: list[dict] = []

        for symbol, df in price_data.items():
            # 防御性：单个 symbol 异常不拖垮全市场扫描（CLAUDE.md 量化风控·边界审查）
            try:
                hit = self._screen_one(symbol, df)
            except Exception as exc:
                # 数据脏值/列缺失等异常 → debug 级记录 symbol+异常类型后跳过，保证
                # 标的池完整性（prod 不污染日志，但保留可追溯线索，杜绝裸静默）。
                _logger.debug("screener 跳过 symbol=%s 异常类型=%s 详情=%s",
                              symbol, type(exc).__name__, exc)
                hit = None
            if hit is not None:
                candidates.append(hit)

        # —— 排序：按近 30 日成交额降序（流动性最好的候选优先）——
        if not candidates:
            # 无命中：返回含完整列名的空 DataFrame（下游 concat/字段访问不报错）
            return pd.DataFrame(columns=[
                "symbol", "pattern_type", "formed_at", "breakout_price",
                "neckline_price", "bottom_price", "depth", "tension",
                "amount30d", "is_valid",
            ])
        out = pd.DataFrame(candidates)
        out = out.sort_values("amount30d", ascending=False, kind="mergesort").reset_index(drop=True)
        return out

    def screen_with_pivots(self, price_data: dict, pivots_map: dict, hv_map: dict, date) -> pd.DataFrame:
        """对标的池用【预算好的 pivots + HV】执行过滤→识别→收集（跳过 compute_atr+causal_pivots+HV 重算）。

        性能优化入口：replay 全 df 一次算 pivots + HV，每 T 截断 iloc[:T+1] + confirm_bars 过滤
        （pivots）+ 尾部 hv_window 窗口（HV）后调本方法，跳过每 T 重算（O(标的×T²)→O(标的×T)）。
        detect + micro_filter 逻辑与 screen() 完全一致。

        参数：
            price_data: {symbol: df.loc[:T]}——截至 T 的截断 df，detect 消费用（量价/ma26w）。
            pivots_map: {symbol: pivots_T}——已 iloc[:T+1] 截断 + confirm_bars 过滤的 pivot Series。
            hv_map: {symbol: hv_win}——截至 T 的尾部 hv_window 个 HV（第三轮 HV 复用）。
            date: 当前交易日（预留，未参与计算，同 screen）。

        无前视保证：pivots_map / hv_map 必须由调用方做截断 + confirm_bars 过滤（模拟 .loc[:T]），
        replay 负责该过滤。
        """
        candidates: list[dict] = []
        for symbol, df in price_data.items():
            try:
                hit = self._screen_one(symbol, df, pivots=pivots_map.get(symbol),
                                       hv_win=hv_map.get(symbol))
            except Exception as exc:
                _logger.debug("screen_with_pivots 跳过 symbol=%s 异常类型=%s 详情=%s",
                              symbol, type(exc).__name__, exc)
                hit = None
            if hit is not None:
                candidates.append(hit)

        if not candidates:
            return pd.DataFrame(columns=[
                "symbol", "pattern_type", "formed_at", "breakout_price",
                "neckline_price", "bottom_price", "depth", "tension",
                "amount30d", "is_valid",
            ])
        out = pd.DataFrame(candidates)
        return out.sort_values("amount30d", ascending=False, kind="mergesort").reset_index(drop=True)

    def _screen_one(self, symbol: str, df: pd.DataFrame, pivots: "pd.Series | None" = None,
                    hv_win: "pd.Series | None" = None) -> dict | None:
        """对单个 symbol 执行完整编排链路，返回候选 dict 或 None（未命中/被过滤）。

        本方法是 screen 的内循环主体，独立出来便于单 symbol 调试与异常隔离。
        所有过滤/识别组件的调用顺序与 screen 文档一致。

        参数：
            pivots: 可选的预算 pivot Series（性能优化用）。传入则跳过 compute_atr+
                causal_pivots 重算（screen_with_pivots 复用入口）；None 则现场重算
                （screen() 旧行为，向后兼容）。两种路径下游 detect 逻辑完全一致。
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
        ok, _reason = self.risk.micro_filter(df, symbol, hv_win=hv_win)
        if not ok:
            return None

        # —— 3. causal_pivots + ATR：因果 ZigZag pivot 提取 ——
        # compute_atr 用 high/low/close 三序列，causal_pivots 基于 close + atr 提取 pivot。
        # 末尾 confirm_bars 内的 pivot 被保守丢弃（未来函数隔离，详见 zigzag_causal.py）。
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]
        if pivots is None:
            # 旧路径（screen() 调用，向后兼容）：现场重算 atr + causal_pivots。
            atr = compute_atr(high, low, close)
            pivots = causal_pivots(close, atr, self.cfg)
        # pivots 已就绪（screen_with_pivots 传入复用，或上方刚算）→ 下游 detect 直接消费，不重算。

        # —— 4. 遍历形态注册表：enable 过滤 + depth 覆写 + detect + 命中收集 ——
        # 注册表驱动（方案B）：把原硬编码的 w/hs/tri 三段 detect + 三套 cfg 覆写收敛为
        # 数据驱动的遍历。新形态只改 caisen/patterns/registry.py 的 PATTERNS，screener 零改。
        hits: list[tuple[PatternMeta, object]] = []
        for meta in PATTERNS:
            # enable 开关过滤（meta.enable_field=None 表示总启用，如 W底/头肩底）
            if meta.enable_field is not None and not getattr(self.cfg, meta.enable_field, True):
                continue
            # depth 覆写：声明了 depth_override_field 则 model_copy 替换 max_pattern_depth
            # （hs 头部幅度 / tri 边长比天然深于 W底颈线高度比，需分类型宽阈值，否则误否决）。
            detect_cfg = self.cfg
            if meta.depth_override_field is not None:
                detect_cfg = self.cfg.model_copy(
                    update={"max_pattern_depth": getattr(self.cfg, meta.depth_override_field)}
                )
            # 单形态异常隔离：一个形态 detect 抛错只跳过该形态，不影响同 symbol 其他形态
            # （粒度细于外层单 symbol 异常隔离，诊断更准——debug 日志标形态名）。
            try:
                res = meta.detect(close, pivots, high, low, volume, detect_cfg)
            except Exception as exc:
                _logger.debug("形态 %s detect 异常 symbol=%s：%s", meta.name, symbol, exc)
                continue
            if res is not None and res.is_valid:
                hits.append((meta, res))

        if not hits:
            return None   # 所有形态均未命中，跳过

        # 多形态命中：取 depth 更大者（满足空间更大；逻辑与原实现一致）
        meta, res = max(hits, key=lambda h: h[1].depth)

        # —— amount30d：近 30 日均成交额（排序键，与流动性过滤同源数据）——
        amount30d = float(df["amount"].tail(30).mean())

        # —— formed_at：形态形成日 = DataFrame index 末值（pivot 末点的交易日）——
        # 用 index 末值而非 pivot 末点 idx，因为 causal_pivots 末尾 confirm_bars 内的 pivot
        # 被丢弃，但形态的"当前形成时点"就是数据末根（T 日收盘看 T-1 及之前，合法）。
        formed_at = df.index[-1]

        # —— breakout_price：颈线突破价（统一用 close.iloc[-1] 代表当前突破状态）——
        # 各形态突破 pivot idx 可能不等于末根（causal_pivots 末尾 confirm_bars 丢弃），
        # 下游 plan.py 计算满足点时用 res.neckline_price + H 重新精算，此处仅排序展示。
        breakout_price = float(close.iloc[-1])

        # —— candidate 构造：通用字段 + extra_output 声明的额外字段 ——
        candidate = {
            "symbol": symbol,
            "pattern_type": meta.name,
            "formed_at": formed_at,
            "breakout_price": breakout_price,
            "neckline_price": float(res.neckline_price),
            "bottom_price": float(res.bottom_price),   # 谷底价由形态直接给出（Bug3 契约）
            "depth": float(res.depth),
            "tension": float(res.tension),
            "amount30d": amount30d,
            "is_valid": True,
        }
        # extra_output：candidate 字段名 → Result 属性名（如 triangle: pattern_height=edge_height，
        # 供 plan.py 满足点计算；W底/头肩底 extra_output={} 无额外字段）。
        for out_field, res_attr in meta.extra_output.items():
            candidate[out_field] = float(getattr(res, res_attr))
        return candidate
