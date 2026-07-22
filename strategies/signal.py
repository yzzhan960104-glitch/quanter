# -*- coding: utf-8 -*-
"""Signal dataclass —— 颈线法信号统一封装（Layer2 阶段1 · 字段口径收敛）。

物理定位：
    收敛颈线法信号历史上两套 dict 字段口径：
      - 回测侧 ``TRADE_REQUIRED_KEYS``（scan_at 产出，含完整进出场闭环字段）；
      - 实盘侧 ``scan_live`` 字段（纯识别，仅 formed_at/neckline/bottom/entry_price/atr）。
    两套原本都是 ``list[dict]``，消费方靠字符串键 ``s["symbol"]`` 读，散落多处且无类型保护。
    本 dataclass 把两套字段并到**同一个 frozen 值对象**里，scan_at / scan_live 统一返
    ``list[Signal]``，signal_runner / backtest_replay 改读 dataclass 属性。

字段设计原则（极简 + 显式）：
    - 字段集 = scan_at ∪ scan_live ∪ 实验归因（_eod 注入）；
    - 两个入口按物理语义填字段，未涉及的字段保持 ``None`` 默认（不强行造值，不撒谎）：
        · scan_at 闭环填 entry_date/exit_date/exit_price/exit_reason/rr/holding_bars 等；
        · scan_live 纯识别只填 formed_at/neckline/bottom/entry_price/atr/breakout_date；
    - 归因字段（experiment_id / experiment_weight）默认值保证老链路零回归：
        experiment_id="" / experiment_weight=1.0（满仓口径）。
    - frozen=True：信号一经产出即不可变（spec §0「参数以不可变快照锁定」红线——
      止损价是实盘风险参数，跨实验串味 = 风险归因错配）。_eod 注入归因用
      ``dataclasses.replace`` 产出新 Signal，不在原对象上原地赋值。

不变量守卫：
    - 决策逻辑零改动：Signal 只封装返回，scan_symbol/scan_at/scan_live 的判定分支不动；
    - signal_runner 行为不变：改读 dataclass 属性后产出的 PlannedOrder 与改前一致
      （由 test_signal_runner* / T1 golden 守）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Signal:
    """颈线法信号值对象（识别 + 进出场 + 实验归因，一字段一义）。

    所有字段均可缺省（None / ""），由各生产方法按物理语义填——未填即代表该信号
    在此入口下不涉及该字段（如 scan_live 不涉及 exit_price，因为实盘 T-1 晚还没有
    未来 K 线来模拟出场）。消费方按各自需要读，缺失字段显式 None 兜底。
    """

    # ---- 标的 + 识别元信息（两入口共用）----
    symbol: str
    """标的代码（如 ``"600000.SH"``）。归因与下单路由的核心 key。"""

    signal_type: str = "neckline"
    """信号类型（颈线法固定 ``"neckline"``；保留字段供未来多策略 registry 分布统计）。"""

    formed_at: Any = None
    """信号形成日（颈线突破日，= detect 窗口末根 ``W.index[-1]``）。
    回测侧是 index label（pd.Timestamp / str）；实盘侧同义。"""

    # ---- 形态识别几何要素（两入口共用，止损/止盈计算依赖）----
    neckline: float | None = None
    """颈线价位 c*（顶部高点聚集定位 + 压制时长验证后的阻力位）。
    stop_price / take_profit 都以颈线为基准 ± N×ATR/H 算。"""

    bottom: float | None = None
    """形态谷底价（窗口最低点 min_price）。H = neckline - bottom 是风险报酬比标尺。"""

    entry_price: float | None = None
    """进场价。scan_at：simulate_exit 算出的挂单回踩成交价；scan_live：颈线价 c_star
    （挂单等回踩，breakout 当日只触发信号不追涨）。"""

    atr: float | None = None
    """信号日的 ATR（窗口对齐 id_cfg["window"]，非写死 14 天）。
    stop_price = neckline - stop_mult × ATR 依赖此值；signal_runner 优先用 signal 自身
    atr（C2 final-fix：防多实验同标的 atr_map 覆盖串味）。"""

    # ---- scan_live 实盘纯识别独有 ----
    breakout_date: Any = None
    """突破日（实盘纯识别用）。detect 内部只在末根突破时返，故 == formed_at；
    显式单列是防御层——未来 detect 若支持历史日回溯，靠此字段过滤只挂当日新信号。"""

    # ---- scan_at 回测一站式独有（simulate_exit 产出的进出场闭环）----
    entry_date: Any = None
    """实际进场日（挂单回踩成交日 buy_date；scan_live 无未来 K 线，不填）。"""

    exit_date: Any = None
    """离场日（止损/止盈/超时触发日）。回测统计 monthly_returns / trades 排序读此。"""

    exit_price: float | None = None
    """离场价（分级止盈两批加权均价，由 avg_pnl 反推）。回测 trades 流水读此。"""

    exit_reason: str | None = None
    """离场原因（stop_loss / tp1 / tp2 / timeout / skip_no_pullback / skip_target_met）。"""

    rr: float | None = None
    """盈亏比（风险倍数）。颈线法口径 = avg_pnl_pct / risk_pct（% / % = 风险倍数），
    与 caisen ``(exit-entry)/(entry-stop)`` 同语义。引擎统计层 win_rate / avg_rr 依赖。"""

    holding_bars: int | None = None
    """持仓交易日数（exit_pos - buy_idx）。引擎统计 avg_holding_bars 读此。"""

    avg_pnl_pct: float | None = None
    """分级止盈 tp1_portion 加权平均收益率（%）。颈线法附加字段，详情展示用。"""

    # ---- 实验归因（_eod 注入，默认满仓口径保证老链路零回归）----
    experiment_id: str = ""
    """所属实验版本 ID（_eod 经 ``dataclasses.replace`` 注入；空串=老链路无归因）。"""

    experiment_weight: float = 1.0
    """资金权重（灰度分流；1.0=满仓口径，向后兼容老 signal_runner 调用）。"""


def signal_to_dict(sig: Signal) -> dict:
    """Signal → dict（兼容老消费方 / JSON 落盘）。

    保留：trading_plan 落盘 / report 聚合等需要序列化的场景仍要 dict；新代码应直接
    读 dataclass 属性。所有字段一次性透出（含 None），不撒谎不省略。
    """
    from dataclasses import asdict
    return asdict(sig)
