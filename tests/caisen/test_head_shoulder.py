# -*- coding: utf-8 -*-
"""头肩底识别测试（蔡森多头买进讯号 · 6 pivot 结构 + 颈线突破）。

覆盖以下用例（仿 Task 6 W 底测试工程教训，所有否决用例均基于已验证的
"峰-谷-峰-谷-峰-谷-峰" 七 pivot 序列构造，确保真实触达被测校验分支，
杜绝 Task 6 review Important#1 踩过的"否决用例假阳性"坑）：

- test_standard_head_shoulder_detected：合成标准头肩底（P4 头底最低 + 右肩≥左肩
  + 颈线突破 + 跨度/幅度/幅宽张力/量价/颈线斜率 全满足，默认量价 0.8/1.5）
  → is_valid=True；
- test_head_not_lowest_rejected + 对照：【形态学硬规则】头底 P4 非整个 P1..P6
  区间最低（左肩 P2 比 P4 还低）→ head_is_lowest 校验否决；对照（抬高 P4 使其
  成为区间最低）→ 通过；
- test_right_shoulder_breaks_left_rejected + 对照：【类推 W底 Task 1 硬规则】
  右肩 P6 明显破左肩 P2（P6 < P2×(1-tolerance)）→ right_above_left 校验否决；
  对照（right_above_left=False 放行）→ 通过；
- test_too_short_span_rejected + 对照：真实产出 6+1 pivot 但跨度 < min_pattern_bars
  → 跨度校验否决；对照（拉长 P1→P2 段使跨度 ≥ 12）→ 通过；
- test_ma26w_filter_rejects + 对照：【类推 Task 1 校准】头底 P4 在 26 周均线之下
  且 ma26w_filter=True → 否决；对照（ma26w_filter=False）→ 通过。

合成序列设计要点（与 causal_pivots 阈值机制对齐，杜绝假阳性）：
  头肩底需要 6 个形态 pivot [P1峰,P2谷左肩,P3峰左颈,P4谷头底,P5峰右颈,P6谷右肩]
  + 1 个突破确认峰 P7。本测试合成序列让 causal_pivots 稳定产出 7 个 pivot，
  尾部 7 pivot 顺序必须是 [1,-1,1,-1,1,-1,1]（峰-谷-峰-谷-峰-谷-峰）。
  detect 从尾部取最后 7 个 pivot，P1=倒数第7、P6=倒数第2（右肩底）、P7=末位（突破峰）。

  causal_pivots 的 thresh = max(0.005, (atr_level/base_price)*zigzag_threshold_atr)。
  本测试用常数 atr=1.0：
    - 短序列 base_price=13 → thresh = max(0.005, 1/13×0.5) ≈ 0.038（3.8%）；
    - 长序列 base_price=50 → thresh ≈ 0.01（1%，更敏感）。
  合成序列的"峰→肩底→颈→头底→颈→肩底→突破"各段幅度均需 > thresh 才能稳定产生 pivot。

  关键不变量（防 review Important#1 假阳性）：每个否决用例的序列均经过 causal_pivots
  实跑验证尾部 7 pivot 顺序为 [1,-1,1,-1,1,-1,1]，且除被测校验外的其他条件（跨度、
  幅度、量价、颈线斜率、头底最低、右肩≥左肩）全部满足，确保被测校验是"唯一否决源"。
"""
import numpy as np
import pandas as pd

from caisen.config import StrategyConfig
from caisen.patterns.zigzag_causal import causal_pivots
from caisen.patterns.head_shoulder import detect, HeadShoulderBottom


def _atr_const(n: int, val: float = 1.0) -> pd.Series:
    """构造常数 ATR 序列（val 元/股），使 thresh 完全由 base_price × cfg 决定。

    base_price=13、atr=1.0、zigzag_threshold_atr=0.5 → thresh=max(0.005, 0.038)=0.038
    （约 3.8%），即 <3.8% 的小波动不构成 pivot，保证合成序列的峰谷落点稳定可预期。
    """
    return pd.Series(val, index=pd.RangeIndex(n), dtype=float)


def _mk_cfg(**overrides) -> StrategyConfig:
    """构造测试用 StrategyConfig（默认量价参数对齐 StrategyConfig 真实默认值）。

    设计意图（与 Task 6 W 底测试对齐 + Task 8 review Important#1 说明）：
      - right_vol_shrink=0.8、breakout_vol_multiplier=1.5 与 StrategyConfig 的真实
        默认值完全一致，避免测试用宽松量价参数掩盖量价校验逻辑的缺陷；
      - 合成序列的 vol 显式构造为"左肩放量 / 右肩缩量(≤80%) / 突破日放量(≥150%)"；
      - min_pattern_bars=11 严格执行蔡森原著"至少 11 根"约束；
      - confirm_bars=2 短确认窗使末段突破 pivot 可被因果确认；
      - w_price_tolerance=0.05（右肩可在左肩 ±5% 内，right_above_left=True 时
        仅约束下限 P6 ≥ P2×(1-0.05)）；
      - ma26w_filter 默认关闭（短合成序列样本不足，由 ma26w 用例专门覆盖）；
      - abc_wave_detect 默认关闭（合成序列区段未必严格 C>A）；
      - max_pattern_depth=1.0：【保留覆盖·非默认】理由——本文件直测 detect 函数本身，
        detect 内部只读 cfg.max_pattern_depth 单一字段做 depth 判定，无 hs_max_pattern_depth
        分类型概念（分类型由 screener 编排层用 model_copy 覆写实现，见 screener.py）。
        合成头肩底 depth≈0.5-0.74（>W 底默认 0.50），若用默认 0.50 会全部否决。故 detect
        单测统一用 1.0 模拟"生产 screener 已用 hs_max_pattern_depth=1.0 覆写"的效果；
        生产侧默认不漏检的回归由 test_screener.py::test_production_default_cfg_detects_standard_w_bottom
        与 test_head_shoulder_wide_depth 共同保证。
    """
    base = dict(
        min_pattern_bars=11,
        max_pattern_bars=60,
        zigzag_threshold_atr=0.5,
        confirm_bars=2,
        w_price_tolerance=0.05,
        min_pattern_depth=0.05,
        max_pattern_depth=1.0,          # 保留覆盖（见 docstring 说明：detect 单测模拟 screener 覆写后的 1.0）
        pattern_tension_ratio=0.05,
        right_vol_shrink=0.8,           # 对齐 StrategyConfig 真实默认值
        breakout_vol_multiplier=1.5,    # 对齐 StrategyConfig 真实默认值
        right_above_left=True,
        ma26w_filter=False,             # 默认关闭 26 周线（短合成序列样本不足）
        abc_wave_detect=False,          # 默认关闭 ABC 波（合成序列区段未必严格 C>A）
    )
    base.update(overrides)
    return StrategyConfig(**base)


def _vol_pattern(n: int, shoulder_i: list, neck_i: list, head_i: int, breakout_i: int) -> pd.Series:
    """构造标准头肩底量价模式（蔡森精準量價：左肩放量 + 头部恐慌量 + 右肩缩量 + 突破放量）。

    物理意图（蔡森原著 + 经典形态学）：
      - 左肩（P2）下杀放量：恐慌性抛售第一阶段，空方力量释放；
      - 头部（P4）放量更甚：主力最后一波洗盘/恐慌底，量可略大于左肩；
      - 颈线段（P3..P5）温和量能：震荡蓄势；
      - 右肩（P6）缩量打底：抛压枯竭，缩量 ≤ 左肩量 × right_vol_shrink（0.8）；
      - 突破日（P7）放量：多方进场，量 ≥ 颈线段均量 × breakout_vol_multiplier（1.5）。

    参数：
        n: 序列总长度；
        shoulder_i: [左肩P2下标, 右肩P6下标]，用于定位放量/缩量位置；
        neck_i: 颈线段 [P3下标, P5下标]，用于计算颈线段均量基准；
        head_i: 头底 P4 下标（主力洗盘放量）；
        breakout_i: 突破日 P7 下标（突破放量）。
    """
    vol = pd.Series(200.0, index=pd.RangeIndex(n))   # 基准温和量能
    left_shoulder_i, right_shoulder_i = shoulder_i
    vol.iloc[left_shoulder_i] = 300.0   # 左肩放量（恐慌下杀第一阶段）
    vol.iloc[head_i] = 350.0            # 头部放量（主力洗盘/恐慌底，略大于左肩）
    # 颈线段（P3..P5）保持温和（200），其均量作为突破日参照基准
    vol.iloc[right_shoulder_i] = 100.0  # 右肩缩量（100 ≤ 300×0.8=240，缩量打底成立）
    vol.iloc[breakout_i] = 500.0        # 突破日放量（500 ≥ 颈线段均量×1.5=300）
    return vol


def _build_standard_head_shoulder() -> tuple:
    """合成标准头肩底序列（头底最低 + 右肩等高左肩 + 颈线突破）。

    序列构造（每段幅度均 >5% 触发 ≈3.8% thresh 的 pivot）：
        起始高位 → 下杀至左肩底 → 反弹至左颈 → 下杀至头底(更低) →
        反弹至右颈(略高于左颈，颈线斜率>0) → 回落至右肩底(等高左肩) →
        突破颈线 → 末尾回踩确认突破 pivot。

    关键：突破高点 P7 必须距序列末尾 ≥ confirm_bars（causal_pivots 末尾隔离要求），
    故在 P7 之后追加 confirm_bars 根回踩 K 线（>5% 回撤触发反转）以确认 P7 峰。

    最终序列（30 根，confirm_bars=2 → P7 后需 2 根确认）：
        [13.0,                                   P1 峰 (idx0) 起始高位
         11.0, 10.0, 9.0, 8.0,                   下杀至 P2 左肩底=8.0 (idx4)
         9.0, 10.0, 11.0, 12.0,                  反弹至 P3 左颈=12.0 (idx8)
         11.0, 10.0, 9.0, 7.0,                   下杀至 P4 头底=7.0 (idx12，区间最低)
         8.0, 9.0, 10.0, 11.0, 12.0, 12.3,       反弹至 P5 右颈=12.3 (idx18，略高于 P3)
         11.0, 10.0, 9.0, 8.0,                   回落至 P6 右肩底=8.0 (idx22，等高左肩)
         9.0, 10.0, 11.0, 12.0, 13.5,            突破至 P7=13.5 (idx27，突破颈线)
         12.5, 12.0]                             末尾回踩（>5% 回撤触发反转确认 P7 峰）
     i:  0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29
    causal_pivots 实测尾部 7 pivot: idx0(1),4(-1),8(1),12(-1),18(1),22(-1),27(1)
        = 峰-谷-峰-谷-峰-谷-峰 ✓
    P1=13,P2=8,P3=12,P4=7,P5=12.3,P6=8,P7=13.5；P4=7 为 P1..P6 区间最低 ✓；
    右肩 P6=8 ≥ 左肩 P2=8 ✓；颈线 P3-P5 在 P7 处价=12.57，P7=13.5 > 颈线 ✓；
    颈线斜率 (12.3-12)/(18-8)=0.03 ≥ 0 ✓；span(P6-P1)=22 > min(11)；
    depth=(12.15-7)/7=0.736（用 max_depth=0.50 会过深，故测试时 max_pattern_depth=1.0 放宽）。
    """
    close = pd.Series(
        [13.0,                                   # P1 峰 (idx0)
         11.0, 10.0, 9.0, 8.0,                   # 下杀至 P2 左肩底=8.0 (idx4)
         9.0, 10.0, 11.0, 12.0,                  # 反弹至 P3 左颈=12.0 (idx8)
         11.0, 10.0, 9.0, 7.0,                   # 下杀至 P4 头底=7.0 (idx12)
         8.0, 9.0, 10.0, 11.0, 12.0, 12.3,       # 反弹至 P5 右颈=12.3 (idx18)
         11.0, 10.0, 9.0, 8.0,                   # 回落至 P6 右肩底=8.0 (idx22)
         9.0, 10.0, 11.0, 12.0, 13.5,            # 突破至 P7=13.5 (idx27)
         12.5, 12.0],                            # 末尾回踩确认 P7 (confirm_bars=2)
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    # 量价：左肩(idx4)放量 + 头底(idx12)放量 + 右肩(idx22)缩量 + 突破(idx27)放量
    vol = _vol_pattern(len(close),
                       shoulder_i=[4, 22], neck_i=[8, 18],
                       head_i=12, breakout_i=27)
    return close, high, low, vol


def _build_head_not_lowest() -> tuple:
    """合成"头底非区间最低"序列（head_is_lowest 否决用例的基准序列）。

    设计意图（防 Task 6 review Important#1 假阳性）：
      头肩底形态学硬规则——P4 头底必须是整个 P1..P6 区间的最低点（头底比两肩都低
      才是"头"）。本序列让左肩 P2=7.0 比头底 P4=8.5 还低（P2 < P4），即"左肩比头
      还低"，结构不成立，head_is_lowest 校验否决。其他条件（跨度、量价、颈线斜率、
      右肩≥左肩）全部满足，唯一否决源是 head_is_lowest。

      注：本序列跨度 span=21 > min(11)，规避 StrategyConfig.min_pattern_bars≥11
      的硬约束，确保 detect 能真实触达 head_is_lowest 校验（而非在跨度校验提前否决）。

    序列（27 根，base_price=13 → thresh≈0.038，各段幅度 >14% >> thresh）：
        [13.0, 12.0, 11.0, 10.0, 9.0, 7.0,    P1峰(0)=13, 跌至 P2左肩底(5)=7.0
         8.0, 9.0, 10.0, 11.0, 12.0,          升至 P3左颈(10)=12
         11.0, 10.0, 9.0, 8.5,                跌至 P4头底(14)=8.5 (高于 P2=7！)
         9.5, 10.5, 11.5, 12.3,               升至 P5右颈(18)=12.3
         11.5, 10.5, 9.5,                     跌至 P6右肩底(21)=9.5 (≥左肩 7.0 通过)
         10.5, 11.5, 12.5, 13.5,              突破至 P7(25)=13.5
         12.5, 12.0]                          末尾回踩
    实测尾部 7 pivot: idx0(1),5(-1),10(1),14(-1),18(1),21(-1),25(1) = 峰-谷-峰-谷-峰-谷-峰 ✓
    P1..P6 = [13,7,12,8.5,12.3,9.5]，区间最低=7(P2)，P4=8.5 非最低 → head_is_lowest 否决。
    """
    close = pd.Series(
        [13.0, 12.0, 11.0, 10.0, 9.0, 7.0,   # P1峰(0)=13, 跌至 P2左肩底(5)=7.0
         8.0, 9.0, 10.0, 11.0, 12.0,         # 升至 P3左颈(10)=12
         11.0, 10.0, 9.0, 8.5,               # 跌至 P4头底(14)=8.5
         9.5, 10.5, 11.5, 12.3,              # 升至 P5右颈(18)=12.3
         11.5, 10.5, 9.5,                    # 跌至 P6右肩底(21)=9.5
         10.5, 11.5, 12.5, 13.5,             # 突破至 P7(25)=13.5
         12.5, 12.0],                        # 回踩确认
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close),
                       shoulder_i=[5, 21], neck_i=[10, 18],
                       head_i=14, breakout_i=25)
    return close, high, low, vol


def _build_head_not_lowest_control() -> tuple:
    """合成"头底最低"对照序列（证明 head_is_lowest 是唯一否决源）。

    设计意图：与 _build_head_not_lowest 同构，但抬高左肩 P2 至 9.5（高于头底 8.5），
    使 P4=8.5 成为 P1..P6 区间最低。若 detect 在此对照上 is_valid=True，则证明
    head_not_lowest 序列的否决唯一来自 head_is_lowest（排除跨度、量价、右肩等干扰）。

    序列（27 根）：
        [13.0, 12.0, 11.0, 10.0, 9.5,        P1峰(0)=13, 跌至 P2左肩底(4)=9.5 (高于头底)
         10.0, 11.0, 12.0,                   升至 P3左颈(7)=12
         11.0, 10.0, 9.0, 8.5,               跌至 P4头底(11)=8.5 (区间最低)
         9.5, 10.5, 11.5, 12.3,              升至 P5右颈(15)=12.3
         11.5, 10.5, 9.5,                    跌至 P6右肩底(18)=9.5 (≥左肩 9.5 通过)
         10.5, 11.5, 12.5, 13.5,             突破 P7(22)=13.5
         12.5, 12.0]                         回踩
    实测尾部 7 pivot: idx0(1),4(-1),7(1),11(-1),15(1),18(-1),22(1) ✓
    P1..P6 = [13,9.5,12,8.5,12.3,9.5]，区间最低=8.5(P4) → head_is_lowest 通过。
    span(P6-P1)=18 > min(11) → 跨度通过。
    """
    close = pd.Series(
        [13.0, 12.0, 11.0, 10.0, 9.5,        # P1峰(0)=13, 跌至 P2左肩底(4)=9.5
         10.0, 11.0, 12.0,                   # 升至 P3左颈(7)=12
         11.0, 10.0, 9.0, 8.5,               # 跌至 P4头底(11)=8.5 (区间最低)
         9.5, 10.5, 11.5, 12.3,              # 升至 P5右颈(15)=12.3
         11.5, 10.5, 9.5,                    # 跌至 P6右肩底(18)=9.5
         10.5, 11.5, 12.5, 13.5,             # 突破 P7(22)=13.5
         12.5, 12.0],                        # 回踩
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close),
                       shoulder_i=[4, 18], neck_i=[7, 15],
                       head_i=11, breakout_i=22)
    return close, high, low, vol


def _build_right_shoulder_breaks_left() -> tuple:
    """合成"右肩破左肩"序列（right_above_left 否决用例的基准序列）。

    设计意图（类推 W底 Task 1 硬规则）：右肩 P6 明显破左肩 P2 下限
    （P6 < P2×(1-tolerance)）→ right_above_left 否决。其他条件全部满足。

      注：本序列跨度 span=17 > min(11)，规避 StrategyConfig.min_pattern_bars≥11
      硬约束，确保 detect 能真实触达 right_above_left 校验。

    序列（23 根）：
        [13.0, 12.0, 11.0, 10.0,            P1峰(0)=13, 跌至 P2左肩底(3)=10
         11.0, 12.0,                        升至 P3左颈(5)=12
         11.0, 10.0, 9.0, 8.5,              跌至 P4头底(9)=8.5 (区间最低)
         9.5, 10.5, 11.5, 12.3,             升至 P5右颈(13)=12.3
         11.5, 10.5, 9.5, 9.0,              跌至 P6右肩底(17)=9.0 (破左脚 10×0.95=9.5)
         10.0, 11.0, 12.0, 13.5,            突破 P7(21)=13.5
         12.5, 12.0]                        末尾回踩
    实测尾部 7 pivot: idx0(1),3(-1),5(1),9(-1),13(1),17(-1),21(1) ✓
    P2=10, P6=9.0 < 10×0.95=9.5 → right_above_left 否决。
    """
    close = pd.Series(
        [13.0, 12.0, 11.0, 10.0,            # P1峰(0)=13, 跌至 P2左肩底(3)=10
         11.0, 12.0,                        # 升至 P3左颈(5)=12
         11.0, 10.0, 9.0, 8.5,              # 跌至 P4头底(9)=8.5
         9.5, 10.5, 11.5, 12.3,             # 升至 P5右颈(13)=12.3
         11.5, 10.5, 9.5, 9.0,              # 跌至 P6右肩底(17)=9.0 (破左脚)
         10.0, 11.0, 12.0, 13.5,            # 突破 P7(21)=13.5
         12.5, 12.0],                       # 回踩确认
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close),
                       shoulder_i=[3, 17], neck_i=[5, 13],
                       head_i=9, breakout_i=21)
    return close, high, low, vol


def _build_too_short_span() -> tuple:
    """合成"跨度不足"序列（跨度否决用例的基准序列）。

    设计意图（防 Task 6 review Important#1 假阳性）：
      原 W底测试用过短序列导致 pivot 不足，detect 在 len(idxs)<4 即 return None，
      从未触达跨度校验 → 假阳性。本序列构造完整 7 pivot（峰-谷-峰-谷-峰-谷-峰），
      但 span(P6-P1)=5 < min_pattern_bars=11，唯一否决源是跨度校验。

    序列（10 根，各段幅度 >20% >> thresh≈3.8%）：
        [13.0, 10.0,       P1峰(0)=13, P2左肩底(1)=10
         12.0,             P3左颈(2)=12
         8.5,              P4头底(3)=8.5
         12.3,             P5右颈(4)=12.3
         10.0,             P6右肩底(5)=10
         11.0, 13.5,       P7突破(7)=13.5
         12.5, 12.0]       回踩确认
    实测尾部 7 pivot: idx0(1),1(-1),2(1),3(-1),4(1),5(-1),7(1) ✓
    span(P6-P1)=5-0=5 < min(11) → 跨度否决。
    """
    close = pd.Series(
        [13.0, 10.0,                          # P1峰(0)=13, P2左肩底(1)=10
         12.0,                                # P3左颈(2)=12
         8.5,                                 # P4头底(3)=8.5
         12.3,                                # P5右颈(4)=12.3
         10.0,                                # P6右肩底(5)=10
         11.0, 13.5,                          # P7突破(7)=13.5
         12.5, 12.0],                         # 回踩确认
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close),
                       shoulder_i=[1, 5], neck_i=[2, 4],
                       head_i=3, breakout_i=7)
    return close, high, low, vol


def _build_too_short_span_control() -> tuple:
    """合成"跨度通过"对照序列（证明跨度是短序列的唯一否决源）。

    设计意图：与 _build_too_short_span 同构，但拉长 P1→P2/P2→P3 段，使 span ≥ 12。
    若 detect 在此对照上 is_valid=True，则证明短序列的否决唯一来自跨度。

    序列（23 根，P1→P2 段拉长至 4 根）：
        [13.0, 12.0, 11.0, 10.0,    P1峰(0)=13, 跌至 P2左肩底(3)=10
         11.0, 12.0,                升至 P3左颈(5)=12
         10.5, 8.5,                 跌至 P4头底(7)=8.5
         10.0, 11.5, 12.3,          升至 P5右颈(10)=12.3
         11.0, 10.0,                跌至 P6右肩底(12)=10
         11.0, 12.0, 13.5,          突破 P7(15)=13.5
         12.5, 12.0]                回踩确认
    实测尾部 7 pivot: idx0(1),3(-1),5(1),7(-1),10(1),12(-1),15(1) ✓
    span(P6-P1)=12-0=12 > min(11) → 跨度通过；其他条件与短序列等价 → 应 is_valid=True。
    """
    close = pd.Series(
        [13.0, 12.0, 11.0, 10.0,             # P1峰(0)=13, 跌至 P2左肩底(3)=10
         11.0, 12.0,                         # P3左颈(5)=12
         10.5, 8.5,                          # P4头底(7)=8.5
         10.0, 11.5, 12.3,                   # P5右颈(10)=12.3
         11.0, 10.0,                         # P6右肩底(12)=10
         11.0, 12.0, 13.5,                   # P7突破(15)=13.5
         12.5, 12.0],                        # 回踩确认
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close),
                       shoulder_i=[3, 12], neck_i=[5, 10],
                       head_i=7, breakout_i=15)
    return close, high, low, vol


def _build_below_ma26w() -> tuple:
    """合成"头底在 26 周线之下"序列（ma26w_filter 否决用例的基准序列）。

    设计意图（类推 Task 1 校准）：
      前段长期下行（均价≈32）使 ma130 在末段处于 ≈31 的高位，末段在 8.5~13.5
      低位构筑标准头肩底（P4 头底最低、右肩等高、颈线突破、颈线斜率>0），头底
      close=8.5 明显低于 ma130=31 → ma26w_filter 唯一否决。

    序列（151 根，base_price=50 → thresh≈0.01，末段各段幅度 >14% >> thresh）：
        前 130 根：linspace(50, 18) 长期下行（均价 34，使 ma130 末段 ≈ 31）；
        后 21 根：在 8.5~13.5 构筑标准头肩底（P1=35@130,P2=10@134,P3=12@136,
                  P4=8.5@139,P5=12.3@142,P6=10@145,P7=13.5@148）。
    实测尾部 7 pivot: idx130(1),134(-1),136(1),139(-1),142(1),145(-1),148(1) ✓
    span(P6-P1)=145-130=15 > min(11)；ma130@P4≈31 > close@P4=8.5 → ma26w_filter 否决。
    """
    n_pre = 130
    # 前段：从 50 缓慢下行至 18（均价约 34，使 ma130 在末段 ≈ 31）
    pre = np.linspace(50.0, 18.0, n_pre).tolist()
    # 末段：先跳升至 35 形成明显 P1 峰（触发向下反转确认前低），再构筑标准头肩底
    tail = [
        35.0,                                    # P1 峰 (idx130) 明显反弹峰
        25.0, 18.0, 12.0, 10.0,                  # 跌至 P2 左肩底 (idx134)=10
        11.0, 12.0,                              # 升至 P3 左颈 (idx136)=12
        11.0, 10.0, 8.5,                         # 跌至 P4 头底 (idx139)=8.5
        9.5, 11.0, 12.3,                         # 升至 P5 右颈 (idx142)=12.3
        11.5, 10.5, 10.0,                        # 跌至 P6 右肩底 (idx145)=10
        11.0, 12.0, 13.5,                        # 突破至 P7 (idx148)=13.5
        12.5, 12.0,                              # 末尾回踩确认 P7
    ]
    close = pd.Series(pre + tail, dtype=float)
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close),
                       shoulder_i=[134, 145], neck_i=[136, 142],
                       head_i=139, breakout_i=148)
    return close, high, low, vol


def _last_n_pivots(piv: pd.Series, n: int = 7) -> list:
    """提取因果 pivot 序列尾部最后 n 个 pivot（用于断言顺序正确性）。"""
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    return [int(piv.iloc[i]) for i in nz[-n:]]


# ---------------------------------------------------------------------------
# 用例 1：标准头肩底识别（默认量价参数 0.8/1.5 下通过）
# ---------------------------------------------------------------------------
def test_standard_head_shoulder_detected():
    """合成标准头肩底（头底最低 + 右肩≥左肩 + 颈线突破）应被识别为 is_valid=True。

    前置断言：causal_pivots 在尾部稳定产出 7 pivot，顺序为
    峰-谷-峰-谷-峰-谷-峰（防 Task 6 review Important#1 假阳性——若顺序错误，
    detect 第一步即 return None，所有后续断言无意义）。
    """
    close, high, low, vol = _build_standard_head_shoulder()
    # 标准序列头底较深(depth=0.736)，放宽 max_pattern_depth 以容纳
    cfg = _mk_cfg(max_pattern_depth=1.0)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置：合成序列应产生 ≥7 个 pivot，且尾部 7 pivot 顺序为 峰-谷-峰-谷-峰-谷-峰
    assert piv.isin([1, -1]).sum() >= 7, f"pivot 不足：{piv.tolist()}"
    assert _last_n_pivots(piv, 7) == [1, -1, 1, -1, 1, -1, 1], \
        f"尾部 7 pivot 顺序错误（应为 峰-谷-峰-谷-峰-谷-峰）：{_last_n_pivots(piv, 7)}"

    res = detect(close, piv, high, low, vol, cfg)
    # 关键断言：识别成功
    assert res is not None, f"未识别头肩底，piv={piv.tolist()}"
    assert isinstance(res, HeadShoulderBottom)
    assert res.is_valid, f"头肩底被判否决：{res.reason}"
    # 结构断言：六点齐全 + 头底最低 + 右肩≥左肩 + 颈线价有效
    assert res.p4_price < res.p2_price, f"头底 {res.p4_price} 应低于左肩 {res.p2_price}"
    assert res.p4_price < res.p6_price, f"头底 {res.p4_price} 应低于右肩 {res.p6_price}"
    assert res.p6_price >= res.p2_price * (1 - cfg.w_price_tolerance), \
        f"右肩 {res.p6_price} 破左肩 {res.p2_price}"
    assert res.depth > 0
    assert res.tension > 0


# ---------------------------------------------------------------------------
# 用例 2：头底非区间最低否决 + 对照（形态学硬规则，唯一否决源证明）
# ---------------------------------------------------------------------------
def test_head_not_lowest_rejected():
    """【形态学硬规则】头底 P4 非整个 P1..P6 区间最低（左肩 P2 比头底还低）→ 否决。

    前置断言：尾部 7 pivot 顺序为 峰-谷-峰-谷-峰-谷-峰，P4 头底非区间最低
    （P2=7 < P4=8.5），其他条件全部满足，证明唯一否决源是 head_is_lowest。
    """
    close, high, low, vol = _build_head_not_lowest()
    # 默认 min_pattern_bars=11，序列 span=21 > 11 通过跨度校验，隔离 head_is_lowest
    cfg = _mk_cfg(max_pattern_depth=1.0)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置 1：尾部 7 pivot 顺序正确（防 Important#1 假阳性）
    assert _last_n_pivots(piv, 7) == [1, -1, 1, -1, 1, -1, 1], \
        f"尾部 7 pivot 顺序错误：{_last_n_pivots(piv, 7)}"
    # 前置 2：P4 确实非区间最低（证明否决源是 head_is_lowest）
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    p_i = nz[-7:-1]   # P1..P6
    p4_i = p_i[3]
    seg_min = float(close.iloc[p_i].min())
    assert close.iloc[p4_i] > seg_min, \
        f"P4={close.iloc[p4_i]} 应高于区间最低 {seg_min}（左肩比头还低）"

    res = detect(close, piv, high, low, vol, cfg)
    # 头底非区间最低 → head_is_lowest 否决
    assert res is None or not res.is_valid, \
        f"P4 非区间最低应被 head_is_lowest 否决（左肩比头还低，非头肩底结构）：{res}"


def test_head_not_lowest_control_passes_when_head_is_lowest():
    """【唯一否决源对照】抬高左肩使 P4 成为区间最低时应通过。

    物理意图：与 test_head_not_lowest_rejected 同构，仅把左肩 P2 从 7.0 抬至 9.5
    （高于头底 8.5），使 P4 成为 P1..P6 区间最低。若 detect 在此对照上 is_valid=True，
    则证明 head_not_lowest 序列的否决唯一来自 head_is_lowest（排除跨度、量价、右肩
    等其他干扰否决）。
    """
    close, high, low, vol = _build_head_not_lowest_control()
    # 默认 min_pattern_bars=11，序列 span=18 > 11 通过；max_pattern_depth 放宽以容纳较深头部
    cfg = _mk_cfg(max_pattern_depth=1.0)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置：尾部 7 pivot 顺序正确 + P4 确为区间最低
    assert _last_n_pivots(piv, 7) == [1, -1, 1, -1, 1, -1, 1], \
        f"对照序列尾部 7 pivot 顺序错误：{_last_n_pivots(piv, 7)}"
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    p_i = nz[-7:-1]
    p4_i = p_i[3]
    seg_min = float(close.iloc[p_i].min())
    assert abs(close.iloc[p4_i] - seg_min) < 1e-9, \
        f"对照 P4={close.iloc[p4_i]} 应等于区间最低 {seg_min}"

    res = detect(close, piv, high, low, vol, cfg)
    # 对照：P4 为区间最低 → 应通过（证明 head_is_lowest 是唯一否决源）
    assert res is not None and res.is_valid, \
        f"P4 为区间最低时应通过（证明否决唯一来自 head_is_lowest），但被否决：{res}"


# ---------------------------------------------------------------------------
# 用例 3：右肩破左肩否决 + 对照（类推 W底 Task 1 硬规则，唯一否决源证明）
# ---------------------------------------------------------------------------
def test_right_shoulder_breaks_left_rejected():
    """【类推 Task 1 硬规则】右肩 P6 破左肩 P2（P6 < P2×(1-tolerance)）→ right_above_left 否决。

    前置断言：尾部 7 pivot 顺序正确，右肩 P6=9.0 明显破左肩 P2=10.0 的下限 9.5，
    其他条件全部满足，证明唯一否决源是 right_above_left。
    """
    close, high, low, vol = _build_right_shoulder_breaks_left()
    # 默认 min_pattern_bars=11，序列 span=17 > 11 通过，隔离 right_above_left 校验
    cfg = _mk_cfg(right_above_left=True, max_pattern_depth=1.0)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置：尾部 7 pivot 顺序正确
    assert _last_n_pivots(piv, 7) == [1, -1, 1, -1, 1, -1, 1], \
        f"尾部 7 pivot 顺序错误：{_last_n_pivots(piv, 7)}"
    # 前置：右肩确实破左肩下限（证明否决源是 right_above_left）
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    p2_i, p6_i = nz[-6], nz[-2]
    p2, p6 = float(close.iloc[p2_i]), float(close.iloc[p6_i])
    assert p6 < p2 * (1 - cfg.w_price_tolerance), \
        f"右肩 {p6} 应破左肩下限 {p2 * (1 - cfg.w_price_tolerance)}"

    res = detect(close, piv, high, low, vol, cfg)
    # 右肩破左肩 → right_above_left 否决
    assert res is None or not res.is_valid, \
        f"右肩破左肩应被 right_above_left 否决，但识别为有效：{res}"


def test_right_shoulder_breaks_left_control_passes_when_disabled():
    """【唯一否决源对照】关闭 right_above_left 后右肩略破左肩应通过。

    物理意图：与 test_right_shoulder_breaks_left_rejected 同一序列，仅把
    right_above_left 从 True 改为 False（退化为 |P6-P2|/P2 ≤ tolerance 双向容忍度）。
    此时 P6=9.0、P2=10.0，|9-10|/10=0.10 > tolerance 0.05 → 仍会被双向容忍度否决。
    故本对照放宽 w_price_tolerance 至 0.15 使 |9-10|/10=0.10 < 0.15 通过，证明
    right_above_left 与容忍度边界是唯一否决源（排除跨度、量价、头底最低等干扰）。
    """
    close, high, low, vol = _build_right_shoulder_breaks_left()
    # 关 right_above_left + 放宽 tolerance 至 0.15 → 右肩 9.0 落入容忍区间
    cfg = _mk_cfg(right_above_left=False,
                  w_price_tolerance=0.15, max_pattern_depth=1.0)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    # 对照：关 right_above_left + 放宽 tolerance → 应通过
    assert res is not None and res.is_valid, \
        f"关 right_above_left + 放宽 tolerance 后应通过（证明否决唯一来自 right_above_left）：{res}"


# ---------------------------------------------------------------------------
# 用例 4：跨度不足否决 + 对照（唯一否决源证明）
# ---------------------------------------------------------------------------
def test_too_short_span_rejected():
    """跨度 < min_pattern_bars → 跨度校验否决（真实触达，非 pivot 不足假阳性）。

    前置断言：causal_pivots 在尾部产出完整的 7 pivot，顺序为
    峰-谷-峰-谷-峰-谷-峰，且跨度 span < min_pattern_bars，证明否决来自跨度校验
    （而非 pivot 不足或顺序错误）。
    """
    close, high, low, vol = _build_too_short_span()
    cfg = _mk_cfg(min_pattern_bars=11, max_pattern_depth=1.0)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置 1：尾部 7 pivot 顺序正确（防顺序错误导致的假阳性）
    assert _last_n_pivots(piv, 7) == [1, -1, 1, -1, 1, -1, 1], \
        f"尾部 7 pivot 顺序错误：{_last_n_pivots(piv, 7)}"
    # 前置 2：跨度确实 < min_pattern_bars（证明否决源是跨度）
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    span = nz[-2] - nz[-7]   # P6_i - P1_i
    assert span < cfg.min_pattern_bars, f"跨度 {span} 应 < min_pattern_bars {cfg.min_pattern_bars}"

    res = detect(close, piv, high, low, vol, cfg)
    # 跨度不足 → 否决
    assert res is None or not res.is_valid, \
        f"短跨度头肩形不应被识别（span={span} < {cfg.min_pattern_bars}）"


def test_too_short_span_control_passes_when_span_sufficient():
    """【唯一否决源对照】跨度足够（span ≥ min_pattern_bars）时同一结构应通过。

    物理意图：与 test_too_short_span_rejected 同构，仅拉长 P1→P2 段使跨度 ≥ 12。
    若 detect 在此对照上 is_valid=True，则证明短序列的否决唯一来自跨度校验
    （排除量价、幅度、右肩、头底最低等其他干扰否决）。
    """
    close, high, low, vol = _build_too_short_span_control()
    cfg = _mk_cfg(min_pattern_bars=11, max_pattern_depth=1.0)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置：尾部 7 pivot 顺序正确 + 跨度足够
    assert _last_n_pivots(piv, 7) == [1, -1, 1, -1, 1, -1, 1], \
        f"对照序列尾部 7 pivot 顺序错误：{_last_n_pivots(piv, 7)}"
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    span = nz[-2] - nz[-7]
    assert span > cfg.min_pattern_bars, f"对照跨度 {span} 应 > min_pattern_bars"

    res = detect(close, piv, high, low, vol, cfg)
    # 对照：跨度足够 → 应通过（证明跨度是短序列的唯一否决源）
    assert res is not None and res.is_valid, \
        f"跨度足够时应通过（证明短序列否决唯一来自跨度），但被否决：{res}"


# ---------------------------------------------------------------------------
# 用例 5：26 周线之下否决 + 对照（类推 Task 1 校准，唯一否决源证明）
# ---------------------------------------------------------------------------
def test_ma26w_filter_rejects_below_ma26w():
    """【类推 Task 1 校准】头底 P4 在 26 周线之下且 ma26w_filter=True 时否决。

    前置断言：尾部 7 pivot 顺序为 峰-谷-峰-谷-峰-谷-峰，头底 close 明显低于
    ma130（26 周线），其他条件全部满足，证明唯一否决源是 ma26w_filter。
    """
    close, high, low, vol = _build_below_ma26w()
    # ma26w_filter=True 开启 26 周线过滤（样本足量 n=151 ≥ ma26w_window=130）
    cfg = _mk_cfg(ma26w_filter=True, ma26w_window=130,
                  min_pattern_bars=11, max_pattern_depth=1.0)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置 1：尾部 7 pivot 顺序正确
    assert _last_n_pivots(piv, 7) == [1, -1, 1, -1, 1, -1, 1], \
        f"尾部 7 pivot 顺序错误：{_last_n_pivots(piv, 7)}"
    # 前置 2：头底确实在 26 周线之下（证明否决源是 ma26w_filter）
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    p4_i = nz[-4]
    ma26w = close.rolling(cfg.ma26w_window, min_periods=cfg.ma26w_window).mean()
    assert close.iloc[p4_i] < ma26w.iloc[p4_i], \
        f"头底 close {close.iloc[p4_i]} 应低于 ma26w {ma26w.iloc[p4_i]}"

    res = detect(close, piv, high, low, vol, cfg)
    # 头底在 26 周线下 → ma26w_filter 否决
    assert res is None or not res.is_valid, \
        f"头底在 26 周线下应被 ma26w_filter 否决：{res}"


def test_ma26w_filter_control_passes_when_filter_disabled():
    """【唯一否决源对照】同一序列关闭 ma26w_filter 后应通过。

    物理意图：与 test_ma26w_filter_rejects_below_ma26w 完全相同的序列，仅把
    ma26w_filter 从 True 改为 False。若 detect 在此对照上 is_valid=True，则证明
    头底在 26 周线之下序列的否决唯一来自 ma26w_filter（排除跨度、幅度、量价、
    右肩、头底最低、颈线斜率等其他干扰否决）。
    """
    close, high, low, vol = _build_below_ma26w()
    # 关闭 ma26w_filter（其他参数与 reject 用例完全一致）
    cfg = _mk_cfg(ma26w_filter=False, ma26w_window=130,
                  min_pattern_bars=11, max_pattern_depth=1.0)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    # 对照：关闭 ma26w_filter → 应通过（证明 ma26w_filter 是唯一否决源）
    assert res is not None and res.is_valid, \
        f"关闭 ma26w_filter 后应通过（证明否决唯一来自 ma26w_filter）：{res}"


# ---------------------------------------------------------------------------
# 用例 6：close[-1] 兜底分支（Task7 review I2 补强——零覆盖兜底分支）
# ---------------------------------------------------------------------------
# 物理意图（head_shoulder.py:231-242 的兜底逻辑）：
#   主路径：P7 > 颈线在 P7 处投影价 → breakout_confirmed=True，直接确认突破；
#   兜底分支：P7 未突破颈线（P7 pivot 可能略早于真实突破日），但序列末根 close[-1]
#             突破颈线 → 仍视为有效突破（用末根处颈线投影价作 neckline_price）；
#   否决分支：P7 未突破且 close[-1] 也未突破 → 形态未确认，return None。
#   原测试 9 用例全部让 P7 直接突破颈线（走主路径），兜底分支零覆盖。本组 2 用例
#   补强：末根突破走兜底确认 / 都未突破走否决。
#
# 合成序列设计要点（与 causal_pivots confirm_bars 机制对齐）：
#   detect 要求尾部正好 7 pivot [峰-谷-峰-谷-峰-谷-峰]，P7=末尾第7=峰。要让 P7
#   成为"已确认的末尾峰"，其后必须有 ≥thresh 下跌来确认反转——但该下跌点本身会
#   变成新 pivot。矛盾解法：让确认 P7 的下跌点落在距末尾 < confirm_bars 的区间被
#   保守丢弃（causal_pivots 红线：末尾未确认 pivot 不标记）。具体：
#     P7@idx26=12.4（峰，颈线投影 12.54，未突破）；
#     idx27=11.7（下跌 5.6% > thresh 3.8% 确认 P7 为峰；但 n=29 时距末尾=1 <
#                confirm_bars=2 → 被 causal_pivots 丢弃，尾部 pivot 数=7 不变）；
#     idx28=末根 close（用例1: 12.75 > 颈线 12.60 突破；用例2: 12.0 < 颈线未突破）。
def _build_p7_no_breakout_close_last_breaks() -> tuple:
    """合成"P7 未突破颈线但末根 close[-1] 突破"序列（兜底确认分支用例的基准序列）。

    设计意图（Task7 review I2）：
      P7=12.4 < 颈线投影 12.54（未突破主路径），但末根 close[-1]=12.75 > 颈线末根
      投影 12.60（兜底确认突破）→ detect 走兜底分支，返回 is_valid=True，且
      neckline_price 重算为末根处投影价 12.60（而非 P7 处 12.54）。

      序列与 _build_standard_head_shoulder 同构（跨度/幅度/量价/头底最低/右肩≥左肩
      全部满足），唯一差异：P7 拉低至 12.4（不突破颈线），末尾追加 1 根 close=12.75
      突破颈线（兜底确认）。

    序列（29 根，base_price=13 → thresh≈0.038，各段幅度 >> thresh）：
        [13.0, 11.0, 10.0, 9.0, 8.0,           P1峰(0)=13, P2左肩底(4)=8
         9.0, 10.0, 11.0, 12.0,                P3左颈(8)=12
         11.0, 10.0, 9.0, 7.0,                 P4头底(12)=7 (区间最低)
         8.0, 9.0, 10.0, 11.0, 12.0, 12.3,     P5右颈(18)=12.3
         11.0, 10.0, 9.0, 8.0,                 P6右肩底(22)=8 (等高左肩)
         9.0, 10.0, 11.0, 12.4,                P7峰(26)=12.4 (未突破颈线12.54)
         11.7,                                 下跌5.6%确认P7峰(距末尾1<confirm_bars=2丢弃)
         12.75]                                末根close=12.75 突破颈线12.60(兜底确认)
    实测尾部 7 pivot: idx0(1),4(-1),8(1),12(-1),18(1),22(-1),26(1) ✓
    P7=12.4 < 颈线@26=12.54（未突破主路径）；close[-1]=12.75 > 颈线@28=12.60（兜底确认）。
    """
    close = pd.Series(
        [13.0, 11.0, 10.0, 9.0, 8.0,            # P1峰(0)=13, P2左肩底(4)=8
         9.0, 10.0, 11.0, 12.0,                 # P3左颈(8)=12
         11.0, 10.0, 9.0, 7.0,                  # P4头底(12)=7
         8.0, 9.0, 10.0, 11.0, 12.0, 12.3,      # P5右颈(18)=12.3
         11.0, 10.0, 9.0, 8.0,                  # P6右肩底(22)=8
         9.0, 10.0, 11.0, 12.4,                 # P7峰(26)=12.4 (未突破颈线)
         11.7,                                  # 下跌确认P7(距末尾<confirm_bars丢弃)
         12.75],                                # 末根close=12.75 突破颈线(兜底确认)
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    # 量价：颈线段(P3=8..P5=18)均量=200（baseline）+ P7(26)=500 放量（≥200×1.5=300）
    vol = _vol_pattern(len(close),
                       shoulder_i=[4, 22], neck_i=[8, 18],
                       head_i=12, breakout_i=26)
    return close, high, low, vol


def _build_p7_no_breakout_close_last_no_break() -> tuple:
    """合成"P7 与 close[-1] 均未突破颈线"序列（无突破否决用例的基准序列）。

    设计意图（Task7 review I2）：
      与 _build_p7_no_breakout_close_last_breaks 同构，唯一差异：末根 close 从 12.75
      改为 12.0（< 颈线末根投影 12.57），即 P7 与 close[-1] 均未突破颈线 → detect
      走否决分支（return None），证明无突破时不识别为头肩底。

    序列（29 根，与 _build_p7_no_breakout_close_last_breaks 仅末根 close 不同）：
        ...（前 28 根同上）
        12.0]                                  末根close=12.0 < 颈线12.57 未突破
    实测尾部 7 pivot: idx0(1),4(-1),8(1),12(-1),18(1),22(-1),26(1) ✓
    P7=12.4 < 颈线@26=12.54；close[-1]=12.0 < 颈线@28=12.57 → 都未突破 → 否决。
    """
    close = pd.Series(
        [13.0, 11.0, 10.0, 9.0, 8.0,            # P1峰(0)=13, P2左肩底(4)=8
         9.0, 10.0, 11.0, 12.0,                 # P3左颈(8)=12
         11.0, 10.0, 9.0, 7.0,                  # P4头底(12)=7
         8.0, 9.0, 10.0, 11.0, 12.0, 12.3,      # P5右颈(18)=12.3
         11.0, 10.0, 9.0, 8.0,                  # P6右肩底(22)=8
         9.0, 10.0, 11.0, 12.4,                 # P7峰(26)=12.4 (未突破颈线)
         11.7,                                  # 下跌确认P7(距末尾<confirm_bars丢弃)
         12.0],                                 # 末根close=12.0 未突破颈线
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close),
                       shoulder_i=[4, 22], neck_i=[8, 18],
                       head_i=12, breakout_i=26)
    return close, high, low, vol


def test_breakout_confirmed_by_last_close():
    """【兜底分支确认】P7 未突破颈线但末根 close[-1] 突破 → 兜底确认，is_valid=True。

    物理意图（head_shoulder.py:231-242 兜底分支）：
      P7 是末尾已确认的突破峰 pivot，但 P7 处 close 可能略早于真实突破日（P7 反转
      确认需要 ≥thresh 下跌，真实突破可能发生在 P7 之后、序列末根）。此时用序列末根
      close[-1] 是否突破颈线兜底判定——突破则形态仍有效，neckline_price 重算为末根
      处投影价（保持与"末根突破"语义一致）。

    前置断言（防 Task 6 review Important#1 假阳性）：
      1. 尾部 7 pivot 顺序为 峰-谷-峰-谷-峰-谷-峰；
      2. P7 close 确实 < 颈线在 P7 处投影价（主路径未突破）；
      3. 末根 close[-1] 确实 > 颈线在末根处投影价（兜底条件成立）。
    满足三者 → 证明 detect 的 is_valid=True 唯一来自兜底分支，而非主路径。
    """
    from caisen.patterns import neckline as neckline_mod
    close, high, low, vol = _build_p7_no_breakout_close_last_breaks()
    cfg = _mk_cfg(max_pattern_depth=1.0)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置 1：尾部 7 pivot 顺序正确
    assert _last_n_pivots(piv, 7) == [1, -1, 1, -1, 1, -1, 1], \
        f"尾部 7 pivot 顺序错误：{_last_n_pivots(piv, 7)}"
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    p7_i, p5_i, p3_i = nz[-1], nz[-3], nz[-5]
    p3, p5, p7 = float(close.iloc[p3_i]), float(close.iloc[p5_i]), float(close.iloc[p7_i])
    neck_at_p7 = neckline_mod.fit_line([(p3_i, p3), (p5_i, p5)], at=p7_i)
    neck_at_end = neckline_mod.fit_line([(p3_i, p3), (p5_i, p5)], at=len(close) - 1)
    # 前置 2：P7 主路径未突破颈线
    assert p7 <= neck_at_p7, \
        f"P7={p7} 应 ≤ 颈线@P7={neck_at_p7:.3f}（主路径未突破，才走兜底分支）"
    # 前置 3：末根 close[-1] 突破颈线（兜底条件成立）
    assert float(close.iloc[-1]) > neck_at_end, \
        f"close[-1]={close.iloc[-1]} 应 > 颈线@end={neck_at_end:.3f}（兜底确认突破）"

    res = detect(close, piv, high, low, vol, cfg)
    # 兜底分支确认 → is_valid=True
    assert res is not None and res.is_valid, \
        f"P7 未突破但 close[-1] 突破时应走兜底确认分支返回 is_valid=True，但得到：{res}"
    # 兜底分支 neckline_price 应为末根处投影价（非 P7 处），证明走了兜底重算路径
    assert abs(res.neckline_price - neck_at_end) < 1e-6, \
        f"兜底分支 neckline_price={res.neckline_price:.6f} 应等于末根处颈线投影 " \
        f"{neck_at_end:.6f}（证明走兜底重算，非主路径）"


def test_no_breakout_rejected():
    """【无突破否决】P7 与 close[-1] 均未突破颈线 → 否决（None 或 is_valid=False）。

    物理意图（head_shoulder.py:236-240 否决分支）：
      P7 未突破颈线，且末根 close[-1] 也未突破 → 形态未被任何方式确认，detect 必须
      否决（return None），证明无突破时不识别为头肩底（杜绝假信号）。

    前置断言：尾部 7 pivot 顺序正确，P7 与 close[-1] 均未突破颈线。
    """
    from caisen.patterns import neckline as neckline_mod
    close, high, low, vol = _build_p7_no_breakout_close_last_no_break()
    cfg = _mk_cfg(max_pattern_depth=1.0)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置 1：尾部 7 pivot 顺序正确
    assert _last_n_pivots(piv, 7) == [1, -1, 1, -1, 1, -1, 1], \
        f"尾部 7 pivot 顺序错误：{_last_n_pivots(piv, 7)}"
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    p7_i, p5_i, p3_i = nz[-1], nz[-3], nz[-5]
    p3, p5, p7 = float(close.iloc[p3_i]), float(close.iloc[p5_i]), float(close.iloc[p7_i])
    neck_at_p7 = neckline_mod.fit_line([(p3_i, p3), (p5_i, p5)], at=p7_i)
    neck_at_end = neckline_mod.fit_line([(p3_i, p3), (p5_i, p5)], at=len(close) - 1)
    # 前置 2：P7 主路径未突破
    assert p7 <= neck_at_p7, \
        f"P7={p7} 应 ≤ 颈线@P7={neck_at_p7:.3f}（主路径未突破）"
    # 前置 3：末根 close[-1] 也未突破（兜底条件不成立 → 应否决）
    assert float(close.iloc[-1]) <= neck_at_end, \
        f"close[-1]={close.iloc[-1]} 应 ≤ 颈线@end={neck_at_end:.3f}（兜底未突破，应否决）"

    res = detect(close, piv, high, low, vol, cfg)
    # P7 与 close[-1] 均未突破 → 否决
    assert res is None or not res.is_valid, \
        f"P7 与 close[-1] 均未突破颈线时应否决（无突破不识别），但得到：{res}"
