# -*- coding: utf-8 -*-
"""W 底识别测试（蔡森多头买进讯号）。

覆盖以下用例（Task 6 review Important#1 重写：所有否决用例均基于已验证的
"谷-峰-谷-峰" 四 pivot 序列构造，确保真实触达被测校验分支，杜绝假阳性）：

- test_standard_w_bottom_detected：合成标准 W 底（右脚≥左脚 + 颈线突破 + 跨度/
  幅度/幅宽张力/量价/颈线斜率 全满足，默认量价参数 0.8/1.5）→ is_valid=True；
- test_too_short_span_rejected + 对照：真实产出 谷-峰-谷-峰 4 pivot 但跨度
  < min_pattern_bars → 跨度校验否决；对照（拉长 P2 段使跨度 ≥ 12）→ 通过；
- test_right_breaks_left_rejected + 对照：【Task 1 硬规则】右脚破左脚
  （P3 < P1×(1-tolerance)）→ right_above_left 校验否决；对照（放宽
  w_price_tolerance 至 0.10 使右脚落入容忍区间）→ 通过；
- test_right_above_left_disabled_allows_lower_right：right_above_left=False 时
  右脚略低于左脚（在 |P3-P1|/P1 ≤ tolerance 双向容忍度内）应被放行；
- test_ma26w_filter_rejects_below_ma26w + 对照：【Task 1 校准】右底在 26 周
  均线之下且 ma26w_filter=True 时否决；对照（ma26w_filter=False）→ 通过；
- test_ma26w_filter_passthrough_when_insufficient_samples：【Task 1 校准·兜底】
  样本不足 ma26w_window 时 ma26w_filter 放行（不阻断）。

合成序列设计要点（与 causal_pivots 阈值机制对齐，杜绝假阳性）：
  causal_pivots 的 thresh = max(0.005, (atr_level/base_price)*zigzag_threshold_atr)。
  本测试用常数 atr=1.0 + 基准价（close.iloc[0]）→ thresh 由 base_price 决定：
    - 短序列 base_price=12~14 → thresh ≈ 0.04~0.05（4~5%）；
    - 长序列 base_price=50  → thresh ≈ 0.01（1%，更敏感）。
  合成 W 底的"左底→颈线→右底→突破"各段幅度均需 > thresh 才能稳定产生 pivot。

  关键不变量（防 review Important#1 假阳性）：detect 从尾部取最后 4 个 pivot
  必须是 谷(-1)-峰(1)-谷(-1)-峰(1)。为此合成序列开头先有一个峰（起始高位），
  使首个 pivot 为 -1（谷），后续交替产出 谷-峰-谷-峰。每个否决用例的序列均
  经过 causal_pivots 实跑验证尾部 4 pivot 顺序正确，且 span、幅度、量价、颈线
  斜率等其他条件全部满足（不构成干扰否决），确保被测校验是"唯一否决源"。
"""
import numpy as np
import pandas as pd
import pytest

from caisen.config import StrategyConfig
from caisen.patterns.zigzag_causal import causal_pivots
from caisen.patterns.w_bottom import detect, WBottom


def _atr_const(n: int, val: float = 1.0) -> pd.Series:
    """构造常数 ATR 序列（val 元/股），使 thresh 完全由 base_price × cfg 决定。

    base_price=12、atr=1.0、zigzag_threshold_atr=0.5 → thresh=max(0.005, 0.0417)=0.0417
    （约 4.2%），即 <5% 的小波动不构成 pivot，保证合成序列的峰谷落点稳定可预期。
    """
    return pd.Series(val, index=pd.RangeIndex(n), dtype=float)


def _mk_cfg(**overrides) -> StrategyConfig:
    """构造测试用 StrategyConfig（默认量价参数对齐 StrategyConfig 真实默认值）。

    设计意图（Task 6 review Minor#2 + Task 8 review Important#1）：
      - right_vol_shrink=0.8、breakout_vol_multiplier=1.5 与 StrategyConfig 的
        真实默认值完全一致，避免测试用宽松量价参数掩盖量价校验逻辑的缺陷；
      - 合成序列的 vol 显式构造为"左底放量 / 右底缩量(≤80%) / 突破日放量(≥150%)"，
        在默认量价参数下标准 W 底即能通过；
      - min_pattern_bars=11 严格执行蔡森原著"至少 11 根"约束；
      - confirm_bars=2 短确认窗使末段突破 pivot 可被因果确认；
      - w_price_tolerance=0.05（右脚可在左脚 ±5% 内，right_above_left=True 时
        仅约束下限 P3 ≥ P1×(1-0.05)）；
      - ma26w_filter 默认关闭（短合成序列样本不足，由兜底用例专门覆盖）；
      - abc_wave_detect 默认关闭（合成序列区段未必严格 C>A）；
      - max_pattern_depth 不覆盖——直接用 StrategyConfig 默认 0.50（Task 8 review
        Important#1 校准：旧默认 0.30 会否决标准 W 底 depth≈0.467）。本测试所有合成
        W 底序列的 depth 均落入 (0.05, 0.50]：标准序列 0.467、right_breaks_left 0.277、
        short_span 0.22、below_ma26w ≈0.20，默认阈值全部通过。
    """
    base = dict(
        min_pattern_bars=11,
        max_pattern_bars=60,
        zigzag_threshold_atr=0.5,
        confirm_bars=2,
        w_price_tolerance=0.05,
        min_pattern_depth=0.05,
        # max_pattern_depth 不覆盖 → 用 StrategyConfig 默认 0.50（合成 W 底 depth ≤ 0.467 通过）
        pattern_tension_ratio=0.05,
        right_vol_shrink=0.8,           # Minor#2：还原为 StrategyConfig 真实默认值
        breakout_vol_multiplier=1.5,    # Minor#2：还原为 StrategyConfig 真实默认值
        right_above_left=True,
        ma26w_filter=False,             # 默认关闭 26 周线（短合成序列样本不足）
        abc_wave_detect=False,          # 默认关闭 ABC 波（合成序列区段未必严格 C>A）
    )
    base.update(overrides)
    return StrategyConfig(**base)


def _vol_pattern(n: int, p1_i: int, p2_i: int, p3_i: int, p4_i: int) -> pd.Series:
    """构造标准 W 底量价模式（蔡森精準量價：左底放量 + 右底缩量 + 突破放量）。

    物理意图（蔡森原著）：
      - 左底（P1）下杀放量：恐慌性抛售，空方力量释放；
      - 右底（P3）缩量打底：抛压枯竭，缩量 ≤ 左底量 × 0.8（right_vol_shrink）；
      - 颈线段（P2..P3）温和量能：震荡蓄势；
      - 突破日（P4）放量：多方进场，量 ≥ 颈线段均量 × 1.5（breakout_vol_multiplier）。

    参数：
        n: 序列总长度；
        p1_i/p2_i/p3_i/p4_i: 四点下标，用于精确定位放量/缩量位置。
    """
    vol = pd.Series(200.0, index=pd.RangeIndex(n))   # 基准温和量能
    vol.iloc[p1_i] = 300.0   # 左底放量（恐慌下杀）
    # P2..P3 颈线段保持温和（200），其均量作为突破日参照基准
    vol.iloc[p3_i] = 100.0   # 右底缩量（100 ≤ 300×0.8=240，缩量打底成立）
    vol.iloc[p4_i] = 500.0   # 突破日放量（500 ≥ 颈线段均量×1.5）
    return vol


def _build_standard_w_bottom() -> tuple:
    """合成标准 W 底序列（右脚略高于左脚 + 颈线突破）。

    序列构造（每段幅度均 >5% 触发 ≈4.2% thresh 的 pivot）：
        高位开盘 → 下跌至左底 → 反弹至颈线高点 → 回落至右底 → 突破颈线
        → 末尾回踩确认突破 pivot。
    关键：突破高点 P4 必须距序列末尾 ≥ confirm_bars（causal_pivots 末尾隔离要求），
    故在 P4 之后追加 confirm_bars 根回踩 K 线（>5% 回撤触发反转）以确认 P4 峰。

    最终序列（20 根，confirm_bars=2 → P4 后需 2 根确认）：
        [12, 11, 10, 9, 8, 7.5,   左侧下跌至左底 P1=7.5 (idx5)
         8, 8.5, 9, 10, 11,       反弹至颈线高点 P2=11 (idx10，多一根使 span>min)
         10, 9, 8.0,              回落至右底 P3=8.0 (idx13，>7.5 右脚抬高)
         9, 10, 11, 13,           突破至 P4=13 (idx17，突破颈线)
         12.5, 12.0]              末尾 2 根回踩（>5% 回撤）触发反转，确认 P4 峰
     i:  0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19
    causal_pivots 实测尾部 4 pivot: idx5(-1),10(1),13(-1),17(1) = 谷-峰-谷-峰 ✓
    跨度=17-5=12 > min(11)；depth=(11-7.5)/7.5=0.467 ∈(0.05,0.5]。
    注：末尾需有 >5% 回撤（13→12.0 跌 7.7%）触发 zigzag 反转，方能在 idx 17 确认峰值；
        单纯持平不会触发反转，P4 无法被 causal_pivots 确认。
    """
    close = pd.Series(
        [12.0, 11.0, 10.0, 9.0, 8.0, 7.5,   # 左侧下跌至左底 P1
         8.0, 8.5, 9.0, 10.0, 11.0,         # 反弹至颈线高点 P2（多一根使 span > min）
         10.0, 9.0, 8.0,                    # 回落至右底 P3（8.0 > 7.5 右脚抬高）
         9.0, 10.0, 11.0, 13.0,             # 突破至 P4（13 > 11 突破颈线）
         12.5, 12.0],                       # 末尾回踩确认 P4（confirm_bars=2）
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    # 量价（_vol_pattern）：左底大量、右底缩量、突破日放量
    vol = _vol_pattern(len(close), p1_i=5, p2_i=10, p3_i=13, p4_i=17)
    return close, high, low, vol


def _build_right_breaks_left() -> tuple:
    """合成"右脚破左脚"序列（Task 1 硬规则否决用例的基准序列）。

    设计意图（Task 6 review Important#1 修复）：
      原 test_right_breaks_left_rejected 的合成序列让 causal_pivots 在尾部产出
      峰-谷-峰-谷（而非 谷-峰-谷-峰），detect 在第一步顺序校验即 return None，
      从未触达 right_above_left 校验 → 假阳性。本序列基于标准 W 底同款构造，
      保证尾部 4 pivot 为 谷-峰-谷-峰，且右底 P3=9.4 明显破左脚 P1=10.0 的
      下限 9.5（=10×(1-0.05)），唯一否决源是 right_above_left。

    序列（19 根，base_price=14 → thresh≈0.036，各段幅度 >5% 稳定触发 pivot）：
        [14, 13, 12, 11, 10,        高位开盘跌至左底 P1=10.0 (idx4)
         10.5, 11, 11.5, 11.8, 12,  反弹至颈线高点 P2=12.0 (idx9)
         11, 10, 9.4,               回落至右底 P3=9.4 (idx12，破左脚下限 9.5)
         10, 11, 12, 13,            突破至 P4=13 (idx16)
         12.5, 12.0]                末尾回踩确认 P4
    实测尾部 4 pivot: idx4(-1),9(1),12(-1),16(1) = 谷-峰-谷-峰 ✓
    span=16-4=12 > min(11)；depth=(12-9.4)/9.4=0.277 ∈(0.05,0.5]；
    tension=2.6/12=0.217 > 0.05；右脚 P3=9.4 < P1×(1-0.05)=9.5 → right_above_left 否决。
    """
    close = pd.Series(
        [14.0, 13.0, 12.0, 11.0, 10.0,          # 高位跌至左底 P1=10 (idx4)
         10.5, 11.0, 11.5, 11.8, 12.0,          # 反弹至颈线 P2=12 (idx9，拉长使 span=12)
         11.0, 10.0, 9.4,                       # 回落至右底 P3=9.4 (idx12，破左脚 6%>5%tol)
         10.0, 11.0, 12.0, 13.0,                # 突破至 P4=13 (idx16)
         12.5, 12.0],                           # 末尾回踩确认 P4
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close), p1_i=4, p2_i=9, p3_i=12, p4_i=16)
    return close, high, low, vol


def _build_short_span() -> tuple:
    """合成"跨度不足"序列（跨度否决用例的基准序列）。

    设计意图（Task 6 review Important#1 修复）：
      原 test_too_short_span_rejected 用 n=5 极短序列，causal_pivots 因 n<5 或
      confirm_bars 丢弃导致 pivot 不足 4 个，detect 在 len(idxs)<4 即 return None，
      从未触达跨度校验 → 假阳性。本序列构造紧凑但完整的 谷-峰-谷-峰 四 pivot，
      span=9 < min_pattern_bars=11，唯一否决源是跨度校验。

    序列（14 根，base_price=14 → thresh≈0.036，各段幅度 >16% 远超 thresh）：
        [14, 12, 10,              高位跌至左底 P1=10 (idx2)
         11, 12, 12.2,            反弹至颈线 P2=12.2 (idx5)
         11, 10.5, 10.0,          回落至右底 P3=10.0 (idx8，右脚等高不破左脚)
         11, 12, 13,              突破至 P4=13 (idx11)
         12.5, 12.0]              末尾回踩确认 P4
    实测尾部 4 pivot: idx2(-1),5(1),7(-1),11(1) = 谷-峰-谷-峰 ✓
    span=11-2=9 < min(11) → 跨度校验否决。
    """
    close = pd.Series(
        [14.0, 12.0, 10.0,                    # 跌至左底 P1=10 (idx2)
         11.0, 12.0, 12.2,                    # 反弹至 P2=12.2 (idx5)
         11.0, 10.5, 10.0,                    # 回落至右底 P3=10 (idx8)
         11.0, 12.0, 13.0,                    # 突破至 P4=13 (idx11)
         12.5, 12.0],                         # 末尾回踩确认 P4
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close), p1_i=2, p2_i=5, p3_i=8, p4_i=11)
    return close, high, low, vol


def _build_short_span_control() -> tuple:
    """合成"跨度通过"对照序列（证明跨度是短序列的唯一否决源）。

    设计意图：与 _build_short_span 同构，但拉长 P1→P2 反弹段，使 span ≥ 12
    （满足 min_pattern_bars < span），其他条件（右脚等高、量价、幅度）不变。
    若 detect 在此对照序列上 is_valid=True，则证明短序列的否决唯一来自跨度。

    序列（19 根，P2 段拉长至 6 根）：
        [14, 12, 10,                  高位跌至左底 P1=10 (idx2)
         10.5, 11, 11.5, 11.8, 12, 12.2,  反弹至颈线 P2=12.2 (idx8)
         11.5, 11, 10.0, 10.0,        回落至右底 P3=10 (idx12)
         11, 12, 13,                  突破至 P4=13 (idx15)
         12.5, 12.0]                  末尾回踩确认 P4
    实测尾部 4 pivot: idx2(-1),8(1),11(-1),15(1) = 谷-峰-谷-峰 ✓
    span=15-2=13 > min(11) → 跨度通过；其他条件与短序列等价 → 应 is_valid=True。
    """
    close = pd.Series(
        [14.0, 12.0, 10.0,                      # 跌至左底 P1=10 (idx2)
         10.5, 11.0, 11.5, 11.8, 12.0, 12.2,    # 反弹至 P2=12.2 (idx8，拉长使 span=13)
         11.5, 11.0, 10.0, 10.0,                # 回落至右底 P3=10 (idx12)
         11.0, 12.0, 13.0,                      # 突破至 P4=13 (idx15)
         12.5, 12.0],                           # 末尾回踩确认 P4
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close), p1_i=2, p2_i=8, p3_i=12, p4_i=15)
    return close, high, low, vol


def _build_below_ma26w() -> tuple:
    """合成"右底在 26 周线之下"序列（ma26w_filter 否决用例的基准序列）。

    设计意图（Task 6 review Important#1 修复）：
      原 test_ma26w_filter_rejects_below_ma26w 的末段 W 底各段幅度不足或顺序
      不符，detect 未触达 ma26w_filter → 假阳性。本序列前段长期下行（均价≈32）
      使 ma130 在末段处于 ≈28 的高位，末段在 10~13 低位构筑标准 W 底（右底等高
      通过 right_above_left），右底 close=10 明显低于 ma130=28.8 → ma26w_filter
      唯一否决。

    序列（149 根，base_price=50 → thresh≈0.01，末段各段幅度 >16% >> thresh）：
        前 130 根：linspace(50, 15) 长期下行（均价 32.5，使 ma130[142]≈28.8）；
        后 19 根：在 10~13 构筑标准 W 底（P1=10@134, P2=12@139, P3=10@141, P4=13@146）。
    实测尾部 4 pivot: idx134(-1),139(1),141(-1),146(1) = 谷-峰-谷-峰 ✓
    span=146-134=12 > min(11)；ma130@141≈28.8 > close@141=10 → ma26w_filter 否决。
    """
    n_pre = 130
    # 前段：从 50 缓慢下行至 15（均价约 32.5），使 ma130 在末段 ≈ 28.8（高位）
    pre = np.linspace(50.0, 15.0, n_pre).tolist()
    # 末段：在 10~13 区间构筑标准 W 底（明显低于 26 周线 ~28.8）
    tail = [
        14.0, 13.0, 12.0, 11.0, 10.0,          # 跌至左底 P1=10 (idx134)
        10.5, 11.0, 11.5, 11.8, 12.0,          # 反弹至颈线 P2=12 (idx139)
        11.0, 10.0, 10.0,                      # 回落至右底 P3=10 (idx142，右脚等高)
        10.5, 11.0, 12.0, 13.0,                # 突破至 P4=13 (idx146)
        12.5, 12.0,                            # 末尾回踩确认 P4
    ]
    close = pd.Series(pre + tail, dtype=float)
    high = close + 0.3
    low = close - 0.3
    vol = _vol_pattern(len(close), p1_i=134, p2_i=139, p3_i=142, p4_i=146)
    return close, high, low, vol


def _last4_pivots(piv: pd.Series) -> list:
    """提取因果 pivot 序列尾部最后 4 个 pivot（用于断言顺序正确性）。"""
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    return [int(piv.iloc[i]) for i in nz[-4:]]


# ---------------------------------------------------------------------------
# 用例 1：标准 W 底识别（默认量价参数 0.8/1.5 下通过）
# ---------------------------------------------------------------------------
def test_standard_w_bottom_detected():
    """合成标准 W 底（右脚≥左脚 + 颈线突破）应被识别为 is_valid=True。

    前置断言：causal_pivots 在尾部稳定产出 谷-峰-谷-峰 四 pivot（防 Important#1
    假阳性——若顺序错误，detect 第一步即 return None，所有后续断言无意义）。
    """
    close, high, low, vol = _build_standard_w_bottom()
    cfg = _mk_cfg()   # 默认量价参数 right_vol_shrink=0.8 / breakout_vol_multiplier=1.5
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置：合成序列应产生 ≥4 个 pivot，且尾部 4 pivot 顺序为 谷-峰-谷-峰
    assert piv.isin([1, -1]).sum() >= 4, f"pivot 不足：{piv.tolist()}"
    assert _last4_pivots(piv) == [-1, 1, -1, 1], \
        f"尾部 4 pivot 顺序错误（应为 谷-峰-谷-峰）：{_last4_pivots(piv)}"

    res = detect(close, piv, high, low, vol, cfg)
    # 关键断言：识别成功
    assert res is not None, f"未识别 W 底，piv={piv.tolist()}"
    assert isinstance(res, WBottom)
    assert res.is_valid, f"W 底被判否决：{res.reason}"
    # 结构断言：四点齐全 + 右脚≥左脚
    assert res.p3_price >= res.p1_price * (1 - cfg.w_price_tolerance), \
        f"右脚 {res.p3_price} 破左脚 {res.p1_price}"
    # 颈线价在两高点之间
    assert res.depth > 0
    assert res.tension > 0


# ---------------------------------------------------------------------------
# 用例 2：跨度不足否决 + 对照（唯一否决源证明）
# ---------------------------------------------------------------------------
def test_too_short_span_rejected():
    """跨度 < min_pattern_bars → 跨度校验否决（真实触达，非 pivot 不足假阳性）。

    前置断言：causal_pivots 在尾部产出完整的 谷-峰-谷-峰 四 pivot，且跨度 span
    < min_pattern_bars，证明否决来自跨度校验（而非 pivot 不足或顺序错误）。
    """
    close, high, low, vol = _build_short_span()
    cfg = _mk_cfg(min_pattern_bars=11)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置 1：尾部 4 pivot 必须是 谷-峰-谷-峰（防顺序错误导致的假阳性）
    assert _last4_pivots(piv) == [-1, 1, -1, 1], \
        f"尾部 4 pivot 顺序错误：{_last4_pivots(piv)}"
    # 前置 2：跨度确实 < min_pattern_bars（证明否决源是跨度）
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    span = nz[-1] - nz[-4]
    assert span < cfg.min_pattern_bars, f"跨度 {span} 应 < min_pattern_bars {cfg.min_pattern_bars}"

    res = detect(close, piv, high, low, vol, cfg)
    # 跨度不足 → 否决
    assert res is None or not res.is_valid, \
        f"短跨度 W 形不应被识别（span={span} < {cfg.min_pattern_bars}）"


def test_too_short_span_control_passes_when_span_sufficient():
    """【唯一否决源对照】跨度足够（span ≥ min_pattern_bars）时同一结构应通过。

    物理意图：与 test_too_short_span_rejected 同构的紧凑 W 底，仅拉长 P1→P2
    反弹段使跨度 ≥ 12。若 detect 在此对照上 is_valid=True，则证明短序列的
    否决唯一来自跨度校验（排除量价、幅度、右脚等其他干扰否决）。
    """
    close, high, low, vol = _build_short_span_control()
    cfg = _mk_cfg(min_pattern_bars=11)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置：尾部 4 pivot 顺序正确 + 跨度足够
    assert _last4_pivots(piv) == [-1, 1, -1, 1], \
        f"对照序列尾部 4 pivot 顺序错误：{_last4_pivots(piv)}"
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    span = nz[-1] - nz[-4]
    assert span > cfg.min_pattern_bars, f"对照跨度 {span} 应 > min_pattern_bars"

    res = detect(close, piv, high, low, vol, cfg)
    # 对照：跨度足够 → 应通过（证明跨度是短序列的唯一否决源）
    assert res is not None and res.is_valid, \
        f"跨度足够时应通过（证明短序列否决唯一来自跨度），但被否决：{res}"


# ---------------------------------------------------------------------------
# 用例 3：右脚破左脚否决 + 对照（Task 1 硬规则，唯一否决源证明）
# ---------------------------------------------------------------------------
def test_right_breaks_left_rejected():
    """【Task 1 硬规则】右脚破左脚（P3 < P1×(1-tolerance)）→ right_above_left 否决。

    前置断言：尾部 4 pivot 顺序为 谷-峰-谷-峰，右脚 P3=9.4 明显破左脚 P1=10.0
    的下限 9.5，其他条件（跨度、幅度、量价、颈线斜率）全部满足，证明唯一否决源
    是 right_above_left 校验（非顺序错误或 pivot 不足导致的假阳性）。
    """
    close, high, low, vol = _build_right_breaks_left()
    cfg = _mk_cfg(right_above_left=True)   # 默认 w_price_tolerance=0.05
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置：尾部 4 pivot 顺序正确（防 Important#1 假阳性）
    assert _last4_pivots(piv) == [-1, 1, -1, 1], \
        f"尾部 4 pivot 顺序错误：{_last4_pivots(piv)}"
    # 前置：右脚确实破左脚下限（证明否决源是 right_above_left）
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    p1_i, p3_i = nz[-4], nz[-2]
    p1, p3 = float(close.iloc[p1_i]), float(close.iloc[p3_i])
    assert p3 < p1 * (1 - cfg.w_price_tolerance), \
        f"右脚 {p3} 应破左脚下限 {p1 * (1 - cfg.w_price_tolerance)}"

    res = detect(close, piv, high, low, vol, cfg)
    # 右脚破左脚 → right_above_left 否决
    assert res is None or not res.is_valid, \
        f"右脚破左脚应被 right_above_left 否决（下跌中继），但识别为有效：{res}"


def test_right_breaks_left_control_passes_when_tolerance_widened():
    """【唯一否决源对照】放宽 w_price_tolerance 使右脚落入容忍区间时应通过。

    物理意图：与 test_right_breaks_left_rejected 同一序列，仅把 w_price_tolerance
    从 0.05 放宽至 0.10（此时 P3=9.4 > P1×(1-0.10)=9.0，右脚不再破位）。若 detect
    在此对照上 is_valid=True，则证明右脚破左脚序列的否决唯一来自 right_above_left
    的容忍度边界（排除跨度、幅度、量价、颈线斜率等其他干扰否决）。

    注：right_above_left=True 与 right_above_left=False 在本对照下等价（P3=9.4
    同时满足单向 9.4>9.0 与双向 |9.4-10|/10=0.06<0.10），故只跑 right_above_left=True。
    """
    close, high, low, vol = _build_right_breaks_left()
    # 放宽 tolerance 至 0.10 → 右脚 9.4 > 10×0.9=9.0，不再破位
    cfg = _mk_cfg(right_above_left=True, w_price_tolerance=0.10)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    # 对照：放宽 tolerance → 应通过（证明 right_above_left 是唯一否决源）
    assert res is not None and res.is_valid, \
        f"放宽 tolerance 后右脚不再破位应通过（证明否决唯一来自 right_above_left）：{res}"


def test_right_above_left_disabled_allows_lower_right():
    """right_above_left=False 时不再强制右脚≥左脚（仅用 |P3-P1|/P1 容忍度）。

    构造右脚略低于左脚但仍在 |P3-P1|/P1 ≤ tolerance 内的 W 底，应被放行。
    本用例显式构造量价（左底放量/右底缩量/突破放量）以满足默认量价参数 0.8/1.5。
    """
    # 左底 8.0、右底 7.7（破 3.75%，< tolerance 5%），关 right_above_left 应放行
    # 末尾加回踩确认 P4（>5% 回撤触发 zigzag 反转）
    close = pd.Series(
        [12.0, 11.0, 10.0, 9.0, 8.0,
         8.5, 9.0, 10.0, 11.0,
         10.5, 9.5, 8.5, 7.7,
         8.5, 9.5, 11.0, 13.0,
         12.0, 11.5],
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    # 显式构造量价（默认参数 0.8/1.5 下需满足：右底缩量 ≤ 左底×0.8、突破放量 ≥ 颈线段均量×1.5）
    # P1=idx4, P2=idx8, P3=idx12, P4=idx16
    vol = _vol_pattern(len(close), p1_i=4, p2_i=8, p3_i=12, p4_i=16)
    cfg = _mk_cfg(right_above_left=False)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    # 关 right_above_left 后，右脚在容忍度内应放行
    assert res is not None and res.is_valid, \
        f"right_above_left=False 时容忍度内右脚略低应放行：piv={piv.tolist()}"


# ---------------------------------------------------------------------------
# 用例 4：26 周线之下否决 + 对照（Task 1 校准，唯一否决源证明）
# ---------------------------------------------------------------------------
def test_ma26w_filter_rejects_below_ma26w():
    """【Task 1 校准】右底在 26 周线之下且 ma26w_filter=True 时否决。

    前置断言：尾部 4 pivot 顺序为 谷-峰-谷-峰，右底 close 明显低于 ma130（26 周线），
    其他条件全部满足，证明唯一否决源是 ma26w_filter（非顺序错误或 pivot 不足）。
    """
    close, high, low, vol = _build_below_ma26w()
    # ma26w_filter=True 开启 26 周线过滤（样本足量 n=149 ≥ ma26w_window=130）
    cfg = _mk_cfg(ma26w_filter=True, ma26w_window=130, min_pattern_bars=11)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    # 前置 1：尾部 4 pivot 顺序正确（防 Important#1 假阳性）
    assert _last4_pivots(piv) == [-1, 1, -1, 1], \
        f"尾部 4 pivot 顺序错误：{_last4_pivots(piv)}"
    # 前置 2：右底确实在 26 周线之下（证明否决源是 ma26w_filter）
    nz = [i for i in range(len(piv)) if piv.iloc[i] != 0]
    p3_i = nz[-2]
    ma26w = close.rolling(cfg.ma26w_window, min_periods=cfg.ma26w_window).mean()
    assert close.iloc[p3_i] < ma26w.iloc[p3_i], \
        f"右底 close {close.iloc[p3_i]} 应低于 ma26w {ma26w.iloc[p3_i]}"

    res = detect(close, piv, high, low, vol, cfg)
    # 右底在 26 周线下 → ma26w_filter 否决
    assert res is None or not res.is_valid, \
        f"右底在 26 周线下应被 ma26w_filter 否决：{res}"


def test_ma26w_filter_control_passes_when_filter_disabled():
    """【唯一否决源对照】同一序列关闭 ma26w_filter 后应通过。

    物理意图：与 test_ma26w_filter_rejects_below_ma26w 完全相同的序列，仅把
    ma26w_filter 从 True 改为 False。若 detect 在此对照上 is_valid=True，则证明
    右底在 26 周线之下序列的否决唯一来自 ma26w_filter（排除跨度、幅度、量价、
    右脚、颈线斜率等其他干扰否决）。
    """
    close, high, low, vol = _build_below_ma26w()
    # 关闭 ma26w_filter（其他参数与 reject 用例完全一致）
    cfg = _mk_cfg(ma26w_filter=False, ma26w_window=130, min_pattern_bars=11)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    # 对照：关闭 ma26w_filter → 应通过（证明 ma26w_filter 是唯一否决源）
    assert res is not None and res.is_valid, \
        f"关闭 ma26w_filter 后应通过（证明否决唯一来自 ma26w_filter）：{res}"


def test_ma26w_filter_passthrough_when_insufficient_samples():
    """【Task 1 校准·兜底】样本不足 ma26w_window 时 ma26w_filter 放行（不阻断）。

    物理意图：26 周线是长期均线，样本不足时无法计算，蔡森原著无明确兜底规则，
    本实现保守放行（避免过度过滤扼杀新上市标的的机会）。
    """
    close, high, low, vol = _build_standard_w_bottom()
    # ma26w_filter=True 但序列长度 20 < ma26w_window=130 → 兜底放行
    cfg = _mk_cfg(ma26w_filter=True, ma26w_window=130)
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = detect(close, piv, high, low, vol, cfg)
    # 样本不足应放行（与关闭 ma26w_filter 等价）
    assert res is not None and res.is_valid, \
        f"样本不足 ma26w_window 时应兜底放行，但被否决：{res}"
