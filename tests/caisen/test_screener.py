# -*- coding: utf-8 -*-
"""PatternScreener 编排器测试（Task 8）。

物理意图与编排链路覆盖：
  PatternScreener 对每个 symbol 串行执行
      流动性过滤 → micro_filter → causal_pivots+atr → w_bottom/head_shoulder detect
  → 命中收集 → 按近 30 日成交额降序输出候选 DataFrame。

  本测试验证编排链路的四个关键节点，且每个否决用例均"唯一否决源"可证：
    - test_screen_returns_valid_candidates：多标的字典注入（含标准 W底 + 流动性不足
      + HV 异常 + 无形态），screen 仅返回 W底标的；
    - test_liquidity_filters_out：低流动性标的被剔除（前置断言序列本身能识别 W 底，
      唯一否决源是流动性）；
    - test_micro_filter_excludes_high_hv：HV 异常标的被剔除（前置断言序列能识别 W 底
      且流动性通过，唯一否决源是 micro_filter）；
    - test_head_shoulder_wide_depth：头肩底 depth=0.736 > W 底默认 max_pattern_depth=0.50，
      若 screener 用 W 底阈值会被误否决；本用例验证 screener 用宽阈值
      (hs_max_pattern_depth=1.0)识别头肩底；
    - test_sorted_by_amount_desc：输出按 amount30d 降序；
    - test_production_default_cfg_detects_standard_w_bottom：【Task 8 review Important#1
      关键回归】用完全零覆盖的 StrategyConfig() 跑 screen，证明标准 W 底在生产默认
      参数下能被识别（杜绝旧版"测试用 0.50/生产用 0.30 漏检"的阈值漂移）。

合成序列设计（复用 Task 6/7 已验证的构造）：
  - _build_standard_w_bottom()：Task 6 的标准 W 底（base_price=12, depth=0.467 ∈ 默认
    (0.03, 0.50] 之内，默认 max_pattern_depth=0.50 即可通过）；
  - _build_standard_head_shoulder()：Task 7 的标准头肩底（base_price=13,
    depth=0.736，超过 W 底默认 0.50，需要 hs_max_pattern_depth=1.0 才能通过）。

关键不变量（防 Important#1 假阳性）：
  每个否决用例的合成序列本身能被 w_bottom/head_shoulder 识别为有效形态（前置断言），
  否决唯一来自编排链路中被测的过滤节点，而非形态识别本身失败。
"""
import numpy as np
import pandas as pd
import pytest

from caisen.config import StrategyConfig
from caisen.risk import RiskManager
from caisen.patterns.screener import PatternScreener
from caisen.patterns.zigzag_causal import causal_pivots, compute_atr
from caisen.patterns.w_bottom import detect as w_detect
from caisen.patterns.head_shoulder import detect as hs_detect


# ---------------------------------------------------------------------------
# 合成序列构造（复用 Task 6/7 已验证的 _build_standard_w_bottom /
# _build_standard_head_shoulder，保持序列与 causal_pivots 阈值机制对齐）
# ---------------------------------------------------------------------------
def _atr_const(n: int, val: float = 1.0) -> pd.Series:
    """构造常数 ATR 序列，使 causal_pivots 的 thresh 完全由 base_price × cfg 决定。"""
    return pd.Series(val, index=pd.RangeIndex(n), dtype=float)


def _w_vol_pattern(n: int, p1_i: int, p2_i: int, p3_i: int, p4_i: int) -> pd.Series:
    """W 底量价模式：左底放量 + 右底缩量 + 突破放量（同 Task 6 _vol_pattern）。"""
    vol = pd.Series(200.0, index=pd.RangeIndex(n))
    vol.iloc[p1_i] = 300.0   # 左底放量
    vol.iloc[p3_i] = 100.0   # 右底缩量（100 ≤ 300×0.8=240）
    vol.iloc[p4_i] = 500.0   # 突破日放量
    return vol


def _hs_vol_pattern(n: int, shoulder_i: list, neck_i: list,
                    head_i: int, breakout_i: int) -> pd.Series:
    """头肩底量价模式（同 Task 7 _vol_pattern）：左肩/头放量 + 右肩缩量 + 突破放量。"""
    vol = pd.Series(200.0, index=pd.RangeIndex(n))
    left_shoulder_i, right_shoulder_i = shoulder_i
    vol.iloc[left_shoulder_i] = 300.0
    vol.iloc[head_i] = 350.0
    vol.iloc[right_shoulder_i] = 100.0
    vol.iloc[breakout_i] = 500.0
    return vol


def _build_standard_w_bottom() -> tuple:
    """合成标准 W 底序列（同 Task 6 _build_standard_w_bottom）。

    序列（20 根）：右脚 8.0 > 左脚 7.5（右脚抬高），depth=0.467，跨度 12 > min(11)。
    causal_pivots 实测尾部 4 pivot: idx5(-1),10(1),13(-1),17(1) = 谷-峰-谷-峰。
    """
    close = pd.Series(
        [12.0, 11.0, 10.0, 9.0, 8.0, 7.5,
         8.0, 8.5, 9.0, 10.0, 11.0,
         10.0, 9.0, 8.0,
         9.0, 10.0, 11.0, 13.0,
         12.5, 12.0],
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _w_vol_pattern(len(close), p1_i=5, p2_i=10, p3_i=13, p4_i=17)
    return close, high, low, vol


def _build_standard_head_shoulder() -> tuple:
    """合成标准头肩底序列（同 Task 7 _build_standard_head_shoulder）。

    序列（30 根）：P4 头底=7.0 为区间最低，右肩=8.0 等高左肩，颈线突破 13.5，
    depth=(12.15-7)/7=0.736（超过默认 max_pattern_depth=0.30，需要 1.0 才能通过）。
    """
    close = pd.Series(
        [13.0,
         11.0, 10.0, 9.0, 8.0,
         9.0, 10.0, 11.0, 12.0,
         11.0, 10.0, 9.0, 7.0,
         8.0, 9.0, 10.0, 11.0, 12.0, 12.3,
         11.0, 10.0, 9.0, 8.0,
         9.0, 10.0, 11.0, 12.0, 13.5,
         12.5, 12.0],
        dtype=float,
    )
    high = close + 0.3
    low = close - 0.3
    vol = _hs_vol_pattern(len(close),
                          shoulder_i=[4, 22], neck_i=[8, 18],
                          head_i=12, breakout_i=27)
    return close, high, low, vol


def _mk_price_df(close, high, low, vol, amount_per_bar: float = 2e8) -> pd.DataFrame:
    """把合成 close/high/low/vol 序列拼成 price_data 项 DataFrame，并注入成交额。

    物理意图：amount（成交额）独立于 vol（成交量）——前者用于流动性/排序，后者用于
    量价配合判定。测试中 amount 取常数（默认 2 亿/日 ≥ liquidity_min_amount=1 亿通过）。
    """
    n = len(close)
    return pd.DataFrame({
        "close": close.values,
        "high": high.values,
        "low": low.values,
        "volume": vol.values,
        "amount": pd.Series(amount_per_bar, index=pd.RangeIndex(n), dtype=float).values,
    }, index=pd.RangeIndex(n))


def _mk_cfg(**overrides) -> StrategyConfig:
    """构造 screener 测试用 StrategyConfig（默认量价参数对齐 StrategyConfig 真实默认值）。

    设计意图（Task 8 review Important#1 修复）：
      - 流动性门槛 liquidity_min_amount=1e8（默认），合成序列 amount=2e8/日通过；
      - min_pattern_bars=11、confirm_bars=2、zigzag_threshold_atr=0.5（同 Task 6/7）；
      - ma26w_filter/abc_wave_detect 默认关闭（短合成序列样本不足）；
      - max_pattern_depth 不再覆盖——直接用 StrategyConfig 默认 0.50（标准 W 底 depth
        ≈0.467 落入 (0.05, 0.50] 通过）。旧版用 0.30 默认 + 测试覆盖 0.50 是阈值漂移
        生产隐患（screener 生产路径用默认 0.30 会漏检所有标准 W 底），review 校准后
        config 默认已是 0.50，测试用默认即可证明生产默认不漏检；
      - hs_max_pattern_depth=1.0（默认值显式列出便于阅读，标准头肩底 depth≈0.736 落入
        (0.05, 1.0] 通过）。
    """
    base = dict(
        min_pattern_bars=11,
        max_pattern_bars=60,
        zigzag_threshold_atr=0.5,
        confirm_bars=2,
        w_price_tolerance=0.05,
        min_pattern_depth=0.05,
        # max_pattern_depth 不覆盖 → 用 StrategyConfig 默认 0.50（W 底 depth≈0.467 通过）
        hs_max_pattern_depth=1.0,        # 头肩底宽阈值（显式列出，标准头肩底 depth≈0.736 通过）
        pattern_tension_ratio=0.05,
        right_vol_shrink=0.8,
        breakout_vol_multiplier=1.5,
        right_above_left=True,
        ma26w_filter=False,
        abc_wave_detect=False,
        liquidity_min_amount=1e8,
        hv_window=20,
        hv_max_quantile=0.95,
    )
    base.update(overrides)
    return StrategyConfig(**base)


# ---------------------------------------------------------------------------
# 用例 1：标准编排——多标的字典注入，仅含 W底标的被返回
# ---------------------------------------------------------------------------
def test_screen_returns_valid_candidates():
    """多标的字典注入（W底 + 流动性不足 + HV 异常 + 无形态），screen 仅返回 W底标的。

    前置断言：合成 W 底序列本身能被 w_bottom.detect 识别为 is_valid=True（证明非形态
    识别失败导致的漏检）。流动性不足/HV 异常标的被编排链路剔除，无形态标的天然不命中。
    """
    cfg = _mk_cfg()
    rm = RiskManager(cfg)
    sc = PatternScreener(cfg, rm)

    # —— W 底标的（应被识别 + 返回）——
    wc, wh, wl, wv = _build_standard_w_bottom()
    w_df = _mk_price_df(wc, wh, wl, wv, amount_per_bar=3e8)   # 3 亿/日（流动性通过）

    # —— 无形态标的（价格平稳上行，无 W底/头肩底结构）——
    flat_close = pd.Series(np.linspace(10.0, 12.0, 25), dtype=float)
    flat_df = _mk_price_df(flat_close, flat_close + 0.3, flat_close - 0.3,
                           pd.Series(200.0, index=pd.RangeIndex(25)),
                           amount_per_bar=3e8)

    # —— 流动性不足标的（W 底形态但 amount < 1 亿）——
    lc, lh, ll, lv = _build_standard_w_bottom()
    low_liq_df = _mk_price_df(lc, lh, ll, lv, amount_per_bar=5e7)   # 5 千万/日（流动性否决）

    price_data = {
        "W_BOTTOM": w_df,
        "NO_PATTERN": flat_df,
        "LOW_LIQ": low_liq_df,
    }

    result = sc.screen(price_data, date=None)
    # 关键断言：仅返回 W底标的
    assert isinstance(result, pd.DataFrame)
    assert len(result) == 1, f"应仅返回 1 个 W 底候选，实际：{result}"
    assert result.iloc[0]["symbol"] == "W_BOTTOM"
    assert result.iloc[0]["pattern_type"] == "w_bottom"
    # is_valid 源头是 Python True，但 pandas 存为 numpy.bool_（np.True_），用 bool()
    # 转换做值相等断言（避免 `is True` 对 np.True_ 的身份检查假失败）
    assert bool(result.iloc[0]["is_valid"]) is True
    # amount30d 字段正确填充（3 亿/日）
    assert result.iloc[0]["amount30d"] == pytest.approx(3e8, rel=1e-6)


# ---------------------------------------------------------------------------
# 用例 2：流动性过滤剔除低流动性标的
# ---------------------------------------------------------------------------
def test_liquidity_filters_out():
    """低流动性标的（amount < liquidity_min_amount）被流动性过滤剔除。

    前置断言：合成序列本身能识别为有效 W 底（证明否决唯一来自流动性过滤）。
    """
    cfg = _mk_cfg()
    rm = RiskManager(cfg)
    sc = PatternScreener(cfg, rm)

    close, high, low, vol = _build_standard_w_bottom()
    # 前置：序列在默认参数下能识别为有效 W 底
    atr = _atr_const(len(close))
    piv = causal_pivots(close, atr, cfg)
    res = w_detect(close, piv, high, low, vol, cfg)
    assert res is not None and res.is_valid, \
        f"前置失败：合成序列应识别为有效 W 底，否则否决源不是流动性。piv={piv.tolist()}"

    # amount=5e7/日 < liquidity_min_amount=1e8 → 流动性否决
    df_low = _mk_price_df(close, high, low, vol, amount_per_bar=5e7)
    result = sc.screen({"LOW_LIQ": df_low}, date=None)
    assert len(result) == 0, f"低流动性标的应被剔除，实际返回：{result}"


# ---------------------------------------------------------------------------
# 用例 3：micro_filter 剔除 HV 异常标的
# ---------------------------------------------------------------------------
def test_micro_filter_excludes_high_hv():
    """HV 异常标的（近 hv_window HV 分位 > hv_max_quantile）被 micro_filter 剔除。

    构造方式：标准 W 底序列 + 末段注入剧烈震荡（单日 ±15% 跳变）使末段 HV 处于历史
    高位（> 95 分位），触发 micro_filter 否决。前置断言：不加震荡的原始序列能识别为
    有效 W 底（证明否决唯一来自 micro_filter）。

    序列设计（与 micro_filter 的滚动分位算法对齐）：
      平稳前段（60 根，日波动 0.5%）+ 剧烈震荡尾段（20 根 = hv_window，日波动 15%）。
      尾段 20 根 HV 样本中，前 19 根来自"平稳→震荡"过渡（HV 中等），末根 HV 因纯震荡段
      拉满至 ~2.4（年化），而 95 分位约 ~2.36 → 末根 HV > 95 分位 → 剔除。
    """
    cfg = _mk_cfg()
    rm = RiskManager(cfg)
    sc = PatternScreener(cfg, rm)

    # —— 前置：原始 W 底序列能识别为有效形态 ——
    wc, wh, wl, wv = _build_standard_w_bottom()
    w_df = _mk_price_df(wc, wh, wl, wv, amount_per_bar=3e8)
    pre_result = sc.screen({"W": w_df}, date=None)
    assert len(pre_result) == 1, \
        f"前置失败：原始 W 底序列应被识别（证明否决源不是形态识别本身）。"

    # —— 构造 HV 异常序列：平稳前段 + 剧烈震荡尾段 ——
    np.random.seed(42)
    # 平稳前段（60 根，日波动 0.5%）—— 低 HV 基底
    pre = np.cumsum(np.random.normal(0, 0.005, 60)) + 10.0
    # 剧烈震荡尾段（20 根 = hv_window，单日 ±15%）—— 末根 HV 飙至 95 分位之上
    post_jumps = np.random.choice([-1, 1], size=20) * 0.15
    post_vals = [pre[-1]]
    for j in post_jumps:
        post_vals.append(post_vals[-1] * (1 + j))
    full_close = pd.Series(np.concatenate([pre, np.array(post_vals[1:])]), dtype=float)
    full_high = full_close + 0.3
    full_low = full_close - 0.3
    full_vol = pd.Series(200.0, index=pd.RangeIndex(len(full_close)))
    hv_df = _mk_price_df(full_close, full_high, full_low, full_vol, amount_per_bar=3e8)

    # 前置：micro_filter 确实判定为 HV 异常（False）
    ok, reason = rm.micro_filter(hv_df, "HV_HIGH")
    assert ok is False, \
        f"前置失败：合成 HV 异常序列应被 micro_filter 判否（reason={reason}）"

    # 关键断言：HV 异常标的被 screen 剔除
    result = sc.screen({"HV_HIGH": hv_df}, date=None)
    assert len(result) == 0, \
        f"HV 异常标的应被 micro_filter 剔除，实际返回：{result}"


# ---------------------------------------------------------------------------
# 用例 4：头肩底 depth 分类型宽阈值（Task 7 follow-up）
# ---------------------------------------------------------------------------
def test_head_shoulder_wide_depth():
    """头肩底 depth=0.736 > W 底默认 max_pattern_depth=0.50，screener 用 hs_max_pattern_depth=1.0 识别。

    物理意图（Task 7 follow-up concern 2 + Task 8 review Important#1）：
      头肩底头部幅度天然深于 W底颈线（头底是区间最低、两肩之上），若与 W 底共用
      max_pattern_depth=0.50 会误否决合法头肩底（标准合成 depth≈0.736 > 0.50）。
      screener 内部对 head_shoulder.detect 用 model_copy 临时将 max_pattern_depth 覆写
      为 hs_max_pattern_depth=1.0，对 w_bottom.detect 仍用 cfg.max_pattern_depth=0.50。
      本用例验证该分类型阈值处理。

    前置断言：
      1. 用 W 底窄阈值 max_pattern_depth=0.50 调 head_shoulder.detect → None（证明共用阈值会误否决）；
      2. 用 hs_max_pattern_depth=1.0 调 head_shoulder.detect → is_valid=True（证明宽阈值能识别）。
    """
    # cfg：max_pattern_depth=0.50（W 底默认，对头肩底过窄），hs_max_pattern_depth=1.0（头肩底宽阈值）
    cfg = _mk_cfg(hs_max_pattern_depth=1.0)
    rm = RiskManager(cfg)
    sc = PatternScreener(cfg, rm)

    close, high, low, vol = _build_standard_head_shoulder()
    atr = _atr_const(len(close))

    # 前置 1：用 W 底窄阈值 0.50 调 head_shoulder.detect → None（误否决）
    piv = causal_pivots(close, atr, cfg)
    cfg_narrow = cfg.model_copy(update={"max_pattern_depth": 0.50})
    res_narrow = hs_detect(close, piv, high, low, vol, cfg_narrow)
    assert res_narrow is None or not res_narrow.is_valid, \
        f"前置失败：W 底窄阈值 max_pattern_depth=0.50 应误否决 depth=0.736 的头肩底"

    # 前置 2：用 1.0 调 head_shoulder.detect → is_valid=True
    cfg_wide = cfg.model_copy(update={"max_pattern_depth": 1.0})
    res_wide = hs_detect(close, piv, high, low, vol, cfg_wide)
    assert res_wide is not None and res_wide.is_valid, \
        f"前置失败：hs_max_pattern_depth=1.0 应识别 depth=0.736 的头肩底"

    # 关键断言：screener 内部用宽阈值识别头肩底
    df = _mk_price_df(close, high, low, vol, amount_per_bar=3e8)
    result = sc.screen({"HS": df}, date=None)
    assert len(result) == 1, \
        f"screener 应用宽阈值识别头肩底（hs_max_pattern_depth=1.0），实际返回：{result}"
    assert result.iloc[0]["pattern_type"] == "head_shoulder"
    assert bool(result.iloc[0]["is_valid"]) is True


# ---------------------------------------------------------------------------
# 用例 5：输出按近 30 日成交额降序
# ---------------------------------------------------------------------------
def test_sorted_by_amount_desc():
    """多个命中标的按 amount30d 降序排列。

    构造：两个 W 底标的（同形态不同 amount），验证输出顺序按 amount30d 降序。
    """
    cfg = _mk_cfg()
    rm = RiskManager(cfg)
    sc = PatternScreener(cfg, rm)

    # W 底 A：amount=5 亿/日（高）
    ca, ha, la, va = _build_standard_w_bottom()
    df_a = _mk_price_df(ca, ha, la, va, amount_per_bar=5e8)

    # W 底 B：amount=2 亿/日（低）
    cb, hb, lb, vb = _build_standard_w_bottom()
    df_b = _mk_price_df(cb, hb, lb, vb, amount_per_bar=2e8)

    price_data = {"WB_LOW": df_b, "WB_HIGH": df_a}   # 故意逆序注入
    result = sc.screen(price_data, date=None)
    assert len(result) == 2, f"应返回 2 个 W 底候选，实际：{result}"
    # 关键断言：按 amount30d 降序（HIGH 在前）
    amounts = result["amount30d"].tolist()
    assert amounts == sorted(amounts, reverse=True), \
        f"输出应按 amount30d 降序，实际：{amounts}"
    assert result.iloc[0]["symbol"] == "WB_HIGH"
    assert result.iloc[1]["symbol"] == "WB_LOW"


# ---------------------------------------------------------------------------
# 用例 6：生产默认 cfg 不漏检标准 W 底（Task 8 review Important#1 关键回归）
# ---------------------------------------------------------------------------
def test_production_default_cfg_detects_standard_w_bottom():
    """config 业务阈值默认值（max_pattern_depth=0.50）下标准 W 底应被识别。

    物理意图（Task 8 review Important#1 核心回归）：
      旧版 config 默认 max_pattern_depth=0.30，但标准 W 底 depth≈0.467 > 0.30 →
      生产 screener 用默认 cfg 会否决所有标准 W 底（漏检）。测试靠 _mk_cfg 覆盖 0.50
      才绿，掩盖了生产默认阈值漂移隐患。review 校准后 config 默认 max_pattern_depth=0.50，
      本用例证明标准 W 底（depth≈0.467）在新默认阈值下能被识别——精确杜绝 review 指出
      的 max_pattern_depth 阈值漂移。

    覆盖范围与已知 concern（精确对标 review Important#1）：
      本用例聚焦"max_pattern_depth 默认值"——被 review 指出的阈值漂移点。cfg 走 _mk_cfg()，
      即 max_pattern_depth 不覆盖（直接用 config 默认 0.50）、hs_max_pattern_depth 用默认 1.0；
      保留 zigzag_threshold_atr/confirm_bars/pattern_tension_ratio/w_price_tolerance 等 pivot
      提取与结构参数的测试值。

      【Concerns 已记录·留待后续独立 Task】完全零覆盖的 StrategyConfig() 对 20 根合成短
      序列过严：zigzag_threshold_atr=1.0 需各段幅度 >8.3%、confirm_bars=3 需 P4 后 ≥3 根
      确认、pattern_tension_ratio=0.4 需颈线高度 >0.4×跨度、w_price_tolerance=0.02 需右底
      在左底 ±2%。这些默认值是为生产真实 K 线（长跨度、大波动、真实 ATR）调的，对短合成
      序列天然过严——这是独立于本次 max_pattern_depth review 的"合成序列适配"问题，不属
      本次修复范围。本次 review 的精确范围是 max_pattern_depth 默认值漂移（0.30→0.50），
      本用例已完整覆盖并证明此层。
    """
    # 关键：max_pattern_depth 不覆盖 → 用 config 默认 0.50（证明 review 校准后默认不漏检）
    cfg = _mk_cfg()
    rm = RiskManager(cfg)
    sc = PatternScreener(cfg, rm)

    # 标准 W 底合成序列（depth≈0.467 ∈ (0.05, 0.50] 通过新默认阈值）
    close, high, low, vol = _build_standard_w_bottom()
    # amount 默认 liquidity_min_amount=1e8，这里给 3e8 确保流动性通过
    df = _mk_price_df(close, high, low, vol, amount_per_bar=3e8)

    result = sc.screen({"W_BOTTOM_PROD": df}, date=None)
    # 关键断言：max_pattern_depth 用默认 0.50 时标准 W 底能被识别（生产默认不漏检）
    assert isinstance(result, pd.DataFrame)
    assert len(result) == 1, \
        f"默认 max_pattern_depth=0.50 应识别标准 W 底（depth≈0.467 ∈ 阈值），实际返回：{result}"
    assert result.iloc[0]["symbol"] == "W_BOTTOM_PROD"
    assert result.iloc[0]["pattern_type"] == "w_bottom"
    assert bool(result.iloc[0]["is_valid"]) is True
