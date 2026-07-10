# -*- coding: utf-8 -*-
"""蔡森形态学策略参数模型（Pydantic，全参数真相源）。

设计意图（CLAUDE.md 极简 + 显式原则）：
- 所有形态识别/计划生成/事前风控阈值集中此模型，严禁逻辑代码硬编码；
- 前端表单可经 `model_json_schema()` 反射动态渲染，避免前后端参数漂移；
- 蔡森方法学专用参数（颈线满足/打底 ABC/右脚>左脚/26 周线/幅宽/破底翻）
  单独分组，便于方法学迭代时定位。

【Task 1 精读校准·覆盖 plan 旧版】
颈线满足计算 = 等额累加级数：目标_n = 颈线 + n × H（H = 颈线到形态最低点的高度），
n 取整数 1/2/3/4，默认 2（看第一 + 第二波满足）。plan 旧版的"倍数语义"已废弃。
"""
from pydantic import BaseModel, Field


class StrategyConfig(BaseModel):
    """蔡森形态学策略全参数模型。

    物理意图分组（每组参数对应方法学的一个独立维度）：
        时间跨度 → 空间高度 → 量价配合 → 交易执行 → 时间止损 → 风控 → 蔡森方法学专用
    所有数值阈值的默认值均来自 `docs/caisen-methodology-summary.md` §9 校准建议。
    """

    # —— 时间跨度类 ——
    min_pattern_bars: int = Field(
        11, ge=11,
        description="形态最小跨度(>10 交易日硬约束；蔡森实战篇：至少 11 根才具结构意义)",
    )
    max_pattern_bars: int = Field(
        60, ge=20, le=120,
        description="形态最大跨度(超过 120 日视为长趋势失效，避免误把大级别回调当打底)",
    )
    symmetry_tolerance: float = Field(
        0.3,
        description="左右结构时间对称容忍度(左脚到颈线 vs 颈线到右脚的耗时占比偏差上限)",
    )

    # —— 空间高度类 ——
    zigzag_threshold_atr: float = Field(
        1.0, ge=0.5,
        description="ZigZag 波段提取阈值(倍 ATR)；自写因果实现亦沿用此阈值过滤噪声波动",
    )
    min_pattern_depth: float = Field(
        0.03,
        description="形态最浅幅度(占价格比例)；<3% 的波动不构成可交易的转折结构",
    )
    max_pattern_depth: float = Field(
        0.50,
        description=(
            "W 底最深幅度阈值（颈线高度比 = (颈线均价 - 两底最低) / 两底最低）。"
            "物理事实（Task 8 review Important#1 校准）：标准合成 W 底 depth≈0.467，"
            "实战 W 底颈线高度比典型分布 0.30~0.60；Task 8 旧默认 0.30 会否决所有"
            "标准 W 底（生产隐患——测试靠 _mk_cfg 覆盖 0.50 才绿，生产 screener 用"
            "默认 0.30 漏检）。改为 0.50 容纳标准 W 底并留 6.7% 余量。W 底判定专用，"
            "头肩底走 hs_max_pattern_depth（头部幅度天然更深，分类型阈值）。"
        ),
    )
    hs_max_pattern_depth: float = Field(
        1.0,
        description=(
            "头肩底 depth 宽阈值（Task 7 follow-up concern 2 + Task 8 review Important#1）。"
            "物理事实：头肩底头部幅度（颈线均价 vs 头底最低）天然深于 W 底颈线高度比——"
            "头底是整个形态区间最低、且两肩之上，故 depth 典型分布 0.50~1.00（标准合成"
            "≈0.736）。若与 W 底共用 max_pattern_depth=0.50 会误否决所有合法头肩底。"
            "PatternScreener 调 head_shoulder.detect 时用 model_copy 临时将 max_pattern_depth"
            "覆写为本字段值（detect 内部只读 cfg.max_pattern_depth，故 screener 用覆写模拟"
            "分类型阈值）；调 w_bottom.detect 时仍用原始 max_pattern_depth。默认 1.0 容纳"
            "标准头肩底（depth≈0.74 留 26% 余量）。"
        ),
    )
    w_price_tolerance: float = Field(
        0.02,
        description="W 底两底价格高度容忍度(右底可略高于/低于左底 2% 内视为等高)",
    )

    # —— 量价配合类（蔡森核心：精準量價）——
    right_vol_shrink: float = Field(
        0.8,
        description="右底缩量比例上限(右底量/左底量 ≤ 0.8 视为缩量打底完成)",
    )
    breakout_vol_multiplier: float = Field(
        1.5,
        description="突破颈线当日成交量放大倍数(≥1.5×近 5 日均量方视为有效突破)",
    )

    # —— 交易执行类 ——
    pullback_window_bars: int = Field(
        3,
        description="突破后有效回踩触发窗口(K 线数)；超窗口未回踩则放弃回踩入场点",
    )
    pullback_max_pct: float = Field(
        0.02, ge=0.0,
        description="回踩入场判定：回踩价不高于突破点 2%(ge=0 防负值，物理无意义)",
    )
    stop_loss_atr_buffer: float = Field(
        0.3,
        description="止损 ATR 额外缓冲(注：蔡森原著止损=C 波低点；此 buffer 仅为日线噪声保险)",
    )
    min_rr_ratio: float = Field(
        3.0,
        description="盈亏比下限(25% 胜率下期望值为正的最低 R/R；低于此值不入场)",
    )

    # —— 时间止损/超时离场 ——
    max_holding_bars: int = Field(
        15,
        description="最大持仓周期(交易日)；超时未达目标则启动离场评估",
    )
    timeout_exit_threshold: float = Field(
        0.01,
        description=(
            "超时砍亏浮盈阈值(持仓达 max_holding_bars 且浮盈【<此值】则砍亏离场；"
            "回测 backtest_replay 与实盘 execution.check_exit 同口径、百分比分母。"
            "【B-3】旧语义「≥此值才允许离场(锁盈)」与实盘相反且虚高回测，已统一为砍亏。"
        ),
    )
    trailing_activation_bars: int = Field(
        5,
        description="移动止盈激活持仓天数(激活后止损随高点上移)",
    )
    trailing_to_breakeven: bool = Field(
        True,
        description="激活移动止盈后是否将止损上移至盈亏平衡点(锁定本金)",
    )

    # —— 风控类 ——
    liquidity_min_amount: float = Field(
        1e8,
        description="近 30 日均成交额下限(1 亿；低于此流动性不足以承载形态交易)",
    )
    hv_window: int = Field(
        20,
        description="历史波动率窗口(20 日标准窗口)",
    )
    hv_max_quantile: float = Field(
        0.95,
        description="HV 异常分位上限(过滤无序震荡行情；当前 HV 处于全市场 95 分位以上则否决)",
    )
    max_position_pct: float = Field(
        0.05,
        description="单标的占总资金上限(5%；集中度风控)",
    )
    macro_regime_veto: bool = Field(
        True,
        description="宏观收缩期是否一票否决新开仓(避免系统性下行中逆势做多)",
    )
    confirm_bars: int = Field(
        3,
        description="ZigZag 末尾 pivot 滞后确认窗口(3 根 K 线后再确认，防末段 pivot 反转)",
    )

    # —— 蔡森方法学专用（Task 1 精读校准，覆盖 plan 旧版倍数语义）——
    neckline_height_multiple: int = Field(
        2, ge=1, le=4,
        description=(
            "颈线满足计算级数 n(等额累加：目标_n = 颈线 + n×H，H=颈线到最低点高度)；"
            "取整数 1..4，默认 2=看第一+第二波满足点。"
            "【Task 1 校准】废弃 plan 旧版的连续倍数语义，改为离散整数级数。"
        ),
    )
    abc_wave_detect: bool = Field(
        True,
        description="启用 ABC 波过程识别(C 波低点 > A 波低点 = 打底完成；防下跌中继误判)",
    )
    right_above_left: bool = Field(
        True,
        description="右脚价 > 左脚价 硬规则(右脚破左脚 = 结构破位，强制否决)",
    )
    ma26w_filter: bool = Field(
        True,
        description="26 周均线打底环境过滤(股价站上 26 周线才视为多头基底)",
    )
    ma26w_window: int = Field(
        130,
        description="26 周均线计算窗口(26 周 ≈ 130 个交易日；与 ma26w_filter 配套)",
    )
    pattern_tension_ratio: float = Field(
        0.4,
        description="幅宽张力比例下限(形态高度/宽度比例；张力不足的扁平结构交易价值低)",
    )
    pattern_width_bonus: bool = Field(
        True,
        description="幅宽加分(打分阶段：形态宽度达阈值的给予 scoring bonus)",
    )
    enable_pot_breakout: bool = Field(
        True,
        description="启用破头锅形态(突破前头部颈线的强势形态)",
    )
    enable_bottom_flip: bool = Field(
        False,
        description="启用破底翻形态(Task 1 新增；默认关，后续按需开启，避免误信号)",
    )
    false_breakout_threshold: float = Field(
        0.03,
        description="假突破跌破颈线阈值(3%；OCR 辨识，可配置；超过此幅度判定假突破)",
    )
    false_breakout_window: int = Field(
        3,
        description="假突破判定窗口(3 日内收回颈线之上视为洗盘而非破位)",
    )
