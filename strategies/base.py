# -*- coding: utf-8 -*-
"""策略中性接口（回测引擎与具体策略解耦的契约层）。

架构原则（2026-07-20 重构）：回测引擎 execution/backtest_replay.py 只做
"逐 symbol×T 滚动调度 + 跨 symbol 聚合统计 + ReplayReport 组装"，不依赖任何具体策略。
策略（caisen 形态 / 颈线法）实现 Strategy Protocol，经 scan_at 一站式产出 trade dict。

出场逻辑归属：策略侧（核心架构决策）。
    颈线法 simulate_exit 是完整状态机（挂单回踩 + max_wait 有效期 + cancel_on 撤单 +
    分级止盈 tp1/tp2 + 超时）；若拆开迁就引擎的"T+1 回踩 + 单笔全平"模型必丢撤单/
    分级减仓语义 = 阉割颈线法。故引擎不感知策略内部如何识别/进场/出场，只接收标准 hit。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd


# 标准 Signal 必填字段集（向后兼容常量 · Layer2 阶段1 后由 Signal dataclass 取代）。
#
# 历史定位：trade dict 字段完整性契约（引擎 _compute_stats 只读这些键）。
# 现状（2026-07-23 · Layer2 阶段1）：scan_at 已返 ``list[Signal]``（frozen dataclass），
# 字段集固化在 ``strategies/signal.Signal`` 类定义里（含回测 TRADE_REQUIRED_KEYS +
# scan_live 实盘字段 + 实验归因），此 set 保留供：
#   ① 向后兼容（外部模块仍 from strategies import TRADE_REQUIRED_KEYS）；
#   ② 文档查阅（一眼看清回测侧必填字段最小集）；
#   ③ 未来若需运行时校验 Signal 是否填齐回测必填项，可基于此 set 派生。
# 新代码不应依赖此 set 做字段判断，应直接读 Signal 属性（类型安全）。
TRADE_REQUIRED_KEYS = {
    "symbol",          # 标的代码
    "signal_type",     # 信号类型（caisen: w_bottom/head_shoulder/...；颈线法: neckline）
    "formed_at",       # 信号形成日 T（index label）
    "entry_date",      # 实际进场日（index label）
    "entry_price",     # 进场价
    "exit_date",       # 离场日（index label）
    "exit_price",      # 离场价
    "exit_reason",     # 离场原因（stop_loss/take_profit/timeout/skip_no_pullback/...）
    "rr",              # 盈亏比（风险倍数 = (exit-entry)/(entry-stop)；颈线法=avg_pnl/risk_pct）
    "holding_bars",    # 持仓交易日数（exit_pos - entry_pos）
}


@runtime_checkable
class Strategy(Protocol):
    """策略中性接口。回测引擎经此与具体策略通信。

    职责切分：
        引擎（backtest_replay.replay）：逐 symbol×T 滚动（无前视 .loc[:T]）、
            abort_cb/progress_cb 调度、跨 symbol 聚合统计、ReplayReport 组装。
        策略（实现本接口）：指标预算（precompute）、信号识别+进场+出场一站式（scan_at）。

    无前视红线：引擎传给 scan_at 的 df_T 严格 = df.loc[:T]；策略不得读取 T 之后的数据
    （precompute 预算的全序列指标，scan_at 内部必须用 .iloc[:T_pos+1] 截断后使用）。
    """

    def precompute(self, symbol: str, full_df: pd.DataFrame) -> dict:
        """在首个 T 前调一次，预算全序列指标（ATR/HV/pivots）供 scan_at 复用。

        返回 strategy_state（策略自定义结构）。scan_at 读它（取预算指标）+ 写它
        （跨 T 状态如去重锚点 last_sig）。颈线法也可在此预算全序列 ATR。
        """
        ...

    def scan_at(
        self,
        symbol: str,
        df_T: pd.DataFrame,
        T,
        strategy_state: dict,
    ) -> list:
        """对单 symbol 在 T 日做"识别 + 进场 + 出场"完整闭环。

        返回 T 日成交的 ``list[Signal]``（0~N 个，每个填齐 TRADE_REQUIRED_KEYS 对应字段）。
        未触发/未成交/被去重跳过 → 返回空列表。引擎把所有非空 hit 汇入统计。

        df_T：严格无前视（= df.loc[:T]）。strategy_state：precompute 初始化，可跨 T 更新。
        """
        ...

    @property
    def config_schema(self) -> type:
        """策略参数 Pydantic 模型类（供 ParamLab 反射 + parse_review 字段护栏）。"""
        ...
