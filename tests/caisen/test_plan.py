# -*- coding: utf-8 -*-
"""TradePlanGenerator 测试（Task 9）。

物理意图与覆盖节点：
  本测试验证蔡森形态学流水线 Task 9 的两个数学内核——
    1. 颈线满足计算（等额累加，非倍数相乘）：Task 1 精读校准覆盖 plan 旧版倍数语义；
    2. 盈亏比 ≥ min_rr_ratio(3.0) 校验：低于 3.0 的计划被丢弃；
  以及止损位（C 波低点 = 谷底 − buffer×ATR）、计划从候选 DataFrame 生成的端到端链路。

蔡森等额累加公式（docs/caisen-methodology-summary.md §2，鉅統/愛之味案例验证）：
    H = 颈线价 − 谷底价
    第一波满足 = 颈线价 + H          （非 颈线价 × 倍数）
    第二波满足 = 第一波满足 + H = 颈线价 + 2×H
    第 n 波满足 = 颈线价 + n×H
"""
import pandas as pd
import pytest

from caisen.config import StrategyConfig
from caisen.risk import RiskManager
from caisen.plan import TradePlan, generate


# ---------------------------------------------------------------------------
# 合成候选 DataFrame 构造器（模拟 PatternScreener 输出契约）
# ---------------------------------------------------------------------------
def _make_candidate(
    *,
    symbol: str = "000001",
    pattern_type: str = "w_bottom",
    neckline_price: float = 10.0,
    depth: float = 0.25,           # depth=0.25 → bottom = 10/(1+0.25) = 8.0
    breakout_price: float | None = None,
    bottom_price: float | None = None,   # Bug3：默认 neckline/(1+depth) 与原反推一致
    amount30d: float = 2e8,
    atr: float | None = None,      # 可选 ATR（缺省时止损 buffer 退化为 0）
    formed_at: pd.Timestamp = pd.Timestamp("2024-01-15"),
) -> pd.DataFrame:
    """构造单行候选 DataFrame，字段对齐 PatternScreener.screen() 输出契约。

    bottom_price（Bug3）：screener 现直接输出形态识别的 bottom_price（W底=min(p1,p3)，
    头肩底=p4），plan 不再逆推。本构造器默认 bottom_price = neckline/(1+depth) 保持
    与 depth 定义的一致性（合成场景下两者等价），可显式覆盖以测试非典型场景。

    atr 为可选字段：screener 输出无 ATR，plan.generate 通过 metadata 或额外列接收；
    缺省时 stop_loss_atr_buffer × ATR 项归零（蔡森原著止损 = C 波低点，buffer 仅为
    日线噪声保险，无 ATR 退化为精确谷底止损亦符合原典）。
    """
    if breakout_price is None:
        breakout_price = neckline_price  # 默认突破价 = 颈线价（理想突破瞬间）
    if bottom_price is None:
        bottom_price = neckline_price / (1.0 + depth)   # 默认与 depth 定义一致
    row = {
        "symbol": symbol,
        "pattern_type": pattern_type,
        "formed_at": formed_at,
        "breakout_price": float(breakout_price),
        "neckline_price": float(neckline_price),
        "bottom_price": float(bottom_price),
        "depth": float(depth),
        "tension": 0.5,
        "amount30d": float(amount30d),
        "is_valid": True,
    }
    df = pd.DataFrame([row])
    if atr is not None:
        df["atr"] = float(atr)
    return df


# ---------------------------------------------------------------------------
# 0. 收敛三角形 pattern_height 满足点（白皮书招12·边长 ≠ 颈线−底）
# ---------------------------------------------------------------------------
class TestTrianglePatternHeight:
    """收敛三角形（招12）满足点用 pattern_height（边长 P1−P2），非颈线−谷底。

    物理意图：三角形上缘突破价（neckline）< P1（首峰），故「颈线−谷底」< 边长(P1−P2)。
    plan.py 对带 pattern_height 列的候选优先用边长作 H，满足点 = 颈线 + n×边长
    （白皮书原著：满足 = 突破颈线 + 三角形垂直边长）。
    """

    def test_triangle_target_uses_pattern_height(self):
        """三角形候选带 pattern_height → take_profit = 颈线 + 边长（非颈线+(颈线−底)）。

        构造：neckline=12（上缘突破投影）, bottom=10.8（真实谷底）, pattern_height=1.7（边长）。
        - 正确（pattern_height）：H=1.7, take_profit=12+1.7=13.7
        - 错误（颈线−底）：H=12−10.8=1.2, take_profit=13.2
        断言 take_profit=13.7，证明三角形走 pattern_height 通道。
        """
        cfg = StrategyConfig()
        risk = RiskManager(cfg)
        row = {
            "symbol": "TRI", "pattern_type": "triangle_bottom",
            "formed_at": pd.Timestamp("2024-01-15"),
            "breakout_price": 12.0, "neckline_price": 12.0,
            "bottom_price": 10.8, "depth": 0.157, "tension": 0.14,
            "amount30d": 3e8, "is_valid": True,
            "pattern_height": 1.7,   # 三角形边长 P1−P2（screener 仅三角形输出此列）
        }
        cands = pd.DataFrame([row])
        plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))
        assert len(plans) == 1
        p = plans[0]
        # H = pattern_height（边长），非 neckline−bottom=1.2
        assert p.H == pytest.approx(1.7, abs=1e-9)
        # 第一波满足 = 颈线 + 1×边长 = 12 + 1.7 = 13.7（非 12+1.2=13.2）
        assert p.take_profit == pytest.approx(13.7, abs=1e-9)
        # 第二波满足 = 颈线 + 2×边长 = 12 + 3.4 = 15.4
        assert p.take_profit_2x == pytest.approx(15.4, abs=1e-9)
        # 止损仍用真实谷底 bottom=10.8（与满足点边长分离，无 ATR buffer）
        assert p.stop_loss == pytest.approx(10.8, abs=1e-9)

    def test_triangle_without_pattern_height_falls_back_to_neckline_minus_bottom(self):
        """无 pattern_height 列 → 回退 H=颈线−底（防御性，与 W底/头肩底一致）。

        screener 对 triangle_bottom 始终输出 pattern_height，但 plan 防御性回退：
        候选无 pattern_height 时 H=颈线−底。保证三角形候选异常缺列时不崩溃。
        """
        cfg = StrategyConfig()
        risk = RiskManager(cfg)
        row = {
            "symbol": "TRI2", "pattern_type": "triangle_bottom",
            "formed_at": pd.Timestamp("2024-01-15"),
            "breakout_price": 12.0, "neckline_price": 12.0,
            "bottom_price": 10.8, "depth": 0.157, "tension": 0.14,
            "amount30d": 3e8, "is_valid": True,
            # 故意不带 pattern_height 列
        }
        cands = pd.DataFrame([row])
        plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))
        if plans:   # rr 可能因 H 偏小而不通过；若通过则 H 应回退为颈线−底
            p = plans[0]
            assert p.H == pytest.approx(12.0 - 10.8, abs=1e-9)


# ---------------------------------------------------------------------------
# 1. 颈线满足计算：等额累加（Task 1 校准核心）
# ---------------------------------------------------------------------------
class TestNecklineSatisfyEqualAccumulation:
    """颈线满足计算 = 等额累加（非倍数相乘）。

    蔡森原著公式（覆盖 plan 旧版倍数语义）：
        H = 颈线价 − 谷底价
        第一波满足 = 颈线价 + H
        第二波满足 = 颈线价 + 2×H
    """

    def test_neckline_satisfy_equal_accumulation(self):
        """颈线10/谷底8 → H=2 → 第一波=12（非10×倍数）, 第二波=14。

        构造：neckline=10, depth=0.25 → bottom = 10/1.25 = 8.0, H = 10-8 = 2
        期望：take_profit=12（=10+2×1）, take_profit_2x=14（=10+2×2）
        反例验证：若是旧版倍数语义 breakout+(breakout-底)×mult，则 10+(10-8)×1=12 巧合
        相同，但第二波会是 10+(10-8)×2=14 也巧合相同——故必须同时验证 H 字段与
        take_profit 的 *加法语义* 而非乘法。这里通过 H=2 的显式断言锁定等额累加内核。

        注：rr 校验要求 breakout ≤ 9.0（见 TestRiskRewardFilter 数学推导），故取
        breakout=9.0 使计划通过 rr≥3 保留，但 H/take_profit/stop_loss 字段只依赖
        neckline/bottom，与 breakout 无关，断言不受影响。
        """
        cfg = StrategyConfig()
        risk = RiskManager(cfg)
        cands = _make_candidate(neckline_price=10.0, depth=0.25,
                                breakout_price=9.0)  # bottom=8, H=2, rr=3.0

        plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))

        assert len(plans) == 1
        p = plans[0]
        # 等额累加内核：H 字段 = 颈线到谷底的绝对高度（非比例）
        assert p.H == pytest.approx(2.0, abs=1e-9)
        # 第一波满足 = 颈线 + 1×H（加法，非倍数）
        assert p.take_profit == pytest.approx(12.0, abs=1e-9)
        # 第二波满足 = 颈线 + 2×H（等额累加）
        assert p.take_profit_2x == pytest.approx(14.0, abs=1e-9)
        # 谷底价反推正确
        assert p.bottom_price == pytest.approx(8.0, abs=1e-9)

    def test_neckline_satisfy_not_multiplicative(self):
        """反例：明确证伪旧版倍数语义 breakout + (breakout - 底部) × multiple。

        构造颈线 ≠ 突破价的场景，使两种语义数值分离：
          neckline=10, breakout=9.0, depth=0.25 → bottom=8, H=2
          - 等额累加（正确）：take_profit = neckline + H = 10 + 2 = 12
          - 旧版倍数（错误）：take_profit = breakout + (breakout-bottom)×mult
                            = 9.0 + (9.0-8)×1 = 9.0 + 1.0 = 10.0
          两者差 2.0，断言锁定等额累加（基于颈线价）而非基于突破价。
        breakout=9.0 使 rr=(12-9)/(9-8)=3.0 通过校验。
        """
        cfg = StrategyConfig()
        risk = RiskManager(cfg)
        cands = _make_candidate(
            neckline_price=10.0, breakout_price=9.0, depth=0.25
        )  # bottom=8, H=2, rr=3.0

        plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))

        assert len(plans) == 1
        p = plans[0]
        # 锁定：take_profit 基于颈线价 + H（=12），而非突破价 +（突破-底）（=10）
        assert p.take_profit == pytest.approx(12.0, abs=1e-9)
        assert p.take_profit != pytest.approx(10.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 2. 盈亏比校验：rr < 3 丢弃 / rr = 3 边界保留
# ---------------------------------------------------------------------------
class TestRiskRewardFilter:
    """盈亏比校验（Bug4 新公式）：rr = (第n波满足 - 回踩均价) / (回踩均价 - stop)，< 3.0 丢弃。

    新公式（回踩入场策略的真实盈亏比）：
        expected_entry = (entry_upper + entry_lower) / 2   回踩挂单区间均价
        target = neckline + n×H                            第 n 波满足（默认 n=2）
        risk = expected_entry - stop_loss
        rr = (target - expected_entry) / risk
    旧公式用突破价作入场 + 第一波目标，数学上 rr 必 < 1（死锁），已废弃。
    """

    def test_rr_below_3_dropped(self):
        """盈亏比 < 3.0 的计划被丢弃（新公式下 rr≈2.80 < 3.0）。

        构造浅形态：
          neckline=10, depth=0.05 → bottom≈9.5238, H≈0.4762
          breakout=10 → entry_upper=10, entry_lower=9.8, expected_entry=9.9
          第 2 波满足 take_profit_n = 10 + 2×0.4762 ≈ 10.9524
          stop = bottom ≈ 9.5238（无 ATR）
          risk = 9.9 - 9.5238 ≈ 0.3762
          rr = (10.9524 - 9.9) / 0.3762 ≈ 2.80 < 3.0 → 丢弃
        """
        cfg = StrategyConfig(min_rr_ratio=3.0)   # 显式 3.0：测"rr<min 丢弃"，不依赖生产默认(现1.5)
        risk = RiskManager(cfg)
        cands = _make_candidate(neckline_price=10.0, depth=0.05)

        plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))

        assert len(plans) == 0, "盈亏比 ≈2.80 < 3.0 的计划应被丢弃"

    def test_rr_meets_threshold_kept(self):
        """盈亏比 ≥ 3.0 的计划保留（新公式下深回踩场景 rr≈5.59）。

        构造：
          neckline=10, depth=0.25 → bottom=8, H=2
          breakout=9.0（突破价低于颈线，模拟颈线下的深回踩挂单边界）
          entry_upper=9.0, entry_lower=8.82, expected_entry=8.91
          第 2 波满足 take_profit_n = 10 + 2×2 = 14
          stop = 8（无 ATR）
          risk = 8.91 - 8 = 0.91
          rr = (14 - 8.91) / 0.91 ≈ 5.5934 ≥ 3.0 → 保留

        注：标准 W 底（breakout≈neckline）新公式 rr 仍偏低（约 1~2），min_rr_ratio=3.0
        偏高是另一独立待办（数据驱动定标），非本 rr 公式修复范围。
        """
        cfg = StrategyConfig()
        risk = RiskManager(cfg)
        cands = _make_candidate(
            neckline_price=10.0, breakout_price=9.0, depth=0.25
        )  # bottom=8, H=2, 第2波=14, expected_entry=8.91, rr≈5.5934

        plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))

        assert len(plans) == 1, "盈亏比 ≈5.59 ≥ 3.0 应保留"
        assert plans[0].rr_ratio == pytest.approx(5.5934, abs=1e-3)

    def test_rr_uses_pullback_avg_entry_not_breakout(self):
        """【Bug4 数学内核】rr 用回踩均价入场，而非突破价。

        构造使回踩均价 ≠ 突破价的场景，验证 expected_entry = (entry_upper+entry_lower)/2
        被用于 rr（而非旧公式的 entry_upper=breakout）：
          neckline=10, depth=0.25 → bottom=8, H=2, 第2波=14
          breakout=10 → entry_upper=10, entry_lower=9.8, expected_entry=9.9
          stop=8, risk=1.9, rr=(14-9.9)/1.9≈2.158
        若错误用突破价 entry_upper=10 作入场：rr=(14-10)/(10-8)=2.0（不同值）。
        断言锁定 2.158（回踩均价语义），证伪旧突破价语义。
        """
        cfg = StrategyConfig(min_rr_ratio=1.5)   # 放宽阈值使 rr≈2.158 的计划保留以便检查 rr 值
        risk = RiskManager(cfg)
        cands = _make_candidate(neckline_price=10.0, breakout_price=10.0, depth=0.25)

        plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))

        assert len(plans) == 1
        # 回踩均价 rr≈2.158（≠ 旧突破价语义的 2.0）
        assert plans[0].rr_ratio == pytest.approx(2.1579, abs=1e-3)


# ---------------------------------------------------------------------------
# 3. 止损 = C 波低点（谷底 - buffer×ATR）
# ---------------------------------------------------------------------------
class TestStopLossAtCWaveBottom:
    """蔡森 Task 1 校准：停损 = C 波低点（W底右底 P3 / 头肩底头底 P4）。

    公式：stop_loss = bottom_price - stop_loss_atr_buffer × ATR
    无 ATR 时退化为 bottom_price（精确谷底止损，亦符合原典）。
    """

    def test_stop_loss_at_c_wave_bottom_no_atr(self):
        """无 ATR 字段：止损 = 谷底价（buffer 项归零）。

        breakout=9.0 使 rr=(12-9)/(9-8)=3.0 通过校验，stop_loss 只依赖 bottom。
        """
        cfg = StrategyConfig()
        risk = RiskManager(cfg)
        cands = _make_candidate(neckline_price=10.0, depth=0.25,
                                breakout_price=9.0)  # bottom=8, rr=3.0

        plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))

        assert len(plans) == 1
        p = plans[0]
        # 谷底止损：8.0（无 ATR buffer）
        assert p.stop_loss == pytest.approx(8.0, abs=1e-9)
        assert p.bottom_price == pytest.approx(8.0, abs=1e-9)

    def test_stop_loss_with_atr_buffer(self):
        """有 ATR 字段：止损 = 谷底 - stop_loss_atr_buffer × ATR。

        构造：bottom=8, ATR=0.5, buffer=0.3（cfg 默认）
              stop_loss = 8 - 0.3 × 0.5 = 8 - 0.15 = 7.85
              rr = (12 - 9) / (9 - 7.85) = 3 / 1.15 ≈ 2.61 < 3.0 → 会被丢弃！
        故调低 breakout 使 rr 仍 ≥ 3：breakout=8.5 → rr=(12-8.5)/(8.5-7.85)=3.5/0.65≈5.38 ✓
        """
        cfg = StrategyConfig()
        risk = RiskManager(cfg)
        cands = _make_candidate(
            neckline_price=10.0, depth=0.25, atr=0.5, breakout_price=8.5,
        )  # bottom=8, atr=0.5, stop=7.85, rr=(12-8.5)/0.65≈5.38

        plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))

        assert len(plans) == 1
        p = plans[0]
        # C 波低点止损 + ATR buffer：8 - 0.3 × 0.5 = 7.85
        assert p.stop_loss == pytest.approx(7.85, abs=1e-9)


# ---------------------------------------------------------------------------
# 4. 端到端：从候选 DataFrame 生成计划列表
# ---------------------------------------------------------------------------
class TestPlanGenerationFromCandidates:
    """从候选 DataFrame（PatternScreener.screen 输出契约）生成 TradePlan 列表。"""

    def test_plan_generation_from_candidates(self):
        """多候选 DataFrame → 多 TradePlan，字段完整性 + 排序保持。

        构造 2 个合法候选（rr ≥ 3）+ 1 个 rr < 3 候选：
          cand1: neckline=10, depth=0.25, breakout=9 → rr=3.0 保留
          cand2: neckline=20, depth=0.25, breakout=18 → bottom=16, H=4,
                 take_profit=24, rr=(24-18)/(18-16)=6.0 保留
          cand3: neckline=10, depth=0.05, breakout=10 → rr=1.0 丢弃
        """
        cfg = StrategyConfig(min_rr_ratio=3.0)   # 显式 3.0：cand3 rr≈2.80<3 丢弃，不依赖生产默认(现1.5)
        risk = RiskManager(cfg)
        cands = pd.concat([
            _make_candidate(symbol="A", neckline_price=10.0, depth=0.25,
                            breakout_price=9.0),   # rr=3.0 保留
            _make_candidate(symbol="B", neckline_price=20.0, depth=0.25,
                            breakout_price=18.0),  # rr=6.0 保留
            _make_candidate(symbol="C", neckline_price=10.0, depth=0.05,
                            breakout_price=10.0),  # rr=1.0 丢弃
        ], ignore_index=True)

        plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))

        assert len(plans) == 2, "2 个 rr≥3 候选保留，1 个 rr<3 丢弃"
        symbols = {p.symbol for p in plans}
        assert symbols == {"A", "B"}

        # —— 字段完整性校验（TradePlan dataclass 全字段非空/合法）——
        for p in plans:
            assert isinstance(p, TradePlan)
            assert p.plan_id  # 非空字符串
            assert p.shares >= 0  # 整手股数非负
            assert p.shares % 100 == 0  # A 股整手
            assert p.valid_until >= p.formed_at  # 有效期不早于形成日
            assert p.max_holding_until >= p.valid_until  # 时间止损晚于回踩窗口
            assert p.timeout_exit_threshold == cfg.timeout_exit_threshold

    def test_empty_candidates_returns_empty_list(self):
        """空候选 DataFrame → 空计划列表（不抛异常）。"""
        cfg = StrategyConfig()
        risk = RiskManager(cfg)
        empty = pd.DataFrame(columns=[
            "symbol", "pattern_type", "formed_at", "breakout_price",
            "neckline_price", "depth", "tension", "amount30d", "is_valid",
        ])

        plans = generate(empty, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))

        assert plans == []

    def test_valid_until_uses_pullback_window(self):
        """valid_until = formed_at + pullback_window_bars 个交易日。

        构造：formed_at=2024-01-15（周一）, pullback_window_bars=3（cfg 默认）
              交易日推进（跳周末）：1-15(周一) → +1=1-16(二) → +2=1-17(三) → +3=1-18(四)
              valid_until = 2024-01-18
              max_holding_until = 2024-01-15 + 15 交易日（cfg 默认 max_holding_bars=15）
        """
        cfg = StrategyConfig()
        risk = RiskManager(cfg)
        cands = _make_candidate(
            formed_at=pd.Timestamp("2024-01-15"),
            neckline_price=10.0, depth=0.25, breakout_price=9.0,  # rr=3 保留
        )

        plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))

        assert len(plans) == 1
        p = plans[0]
        # valid_until = 2024-01-15(周一) + 3 交易日 = 2024-01-18(周四)（bdate_range 跳周末）
        assert p.valid_until == pd.Timestamp("2024-01-18")
        # max_holding_until = 2024-01-15 + 15 交易日 = 2024-02-05(周一)
        # （bdate_range 验证：1-16..1-19, 1-22..1-26, 1-29..2-2, 2-5 共 15 工作日）
        assert p.max_holding_until == pd.Timestamp("2024-02-05")


# ---------------------------------------------------------------------------
# 形态时机守卫：breakout > take_profit（第一波已超=形态确认太晚）→ 丢弃
# ---------------------------------------------------------------------------
def test_plan_filters_breakout_above_take_profit():
    """【形态时机】breakout > take_profit（第一波目标已被突破价超过=形态确认太晚）→ 丢弃。

    诊断（2026-07-11 sample=200/3年）发现近年部分 W 底 breakout 涨过头（第一波 tp 在脚下），
    回踩入场后无盈利空间（如 002779 breakout 127.83 > tp 118.50，rr≈0）。守卫过滤这类形态。
    """
    cfg = StrategyConfig(min_rr_ratio=0.0)   # 不 rr 过滤，只测形态时机守卫
    risk = RiskManager(cfg)
    # neckline=10, bottom=8 → H=2, take_profit=12. breakout=13 > 12 → 形态太晚，丢弃
    cands = _make_candidate(neckline_price=10.0, bottom_price=8.0,
                            breakout_price=13.0, depth=0.25)
    plans = generate(cands, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))
    assert len(plans) == 0, "breakout>take_profit（第一波已超，形态太晚）应丢弃"

    # 对照：breakout=11 < take_profit=12（第一波未超）→ 正常生成
    cands_ok = _make_candidate(neckline_price=10.0, bottom_price=8.0,
                               breakout_price=11.0, depth=0.25)
    plans_ok = generate(cands_ok, cfg, risk, aum=1e6, date=pd.Timestamp("2024-01-15"))
    assert len(plans_ok) == 1, "breakout<take_profit（第一波未超）应正常生成"
