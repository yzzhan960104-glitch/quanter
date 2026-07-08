# -*- coding: utf-8 -*-
"""事前风控：宏观仓位系数 + 微观波动率过滤 + 流动性 + 仓位分配。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本模块决定"能否开、开多大、标的是否被过滤"，在筛形态阶段执行（事前）。
    与既有 risk_shield.check_order（事中拦废单）、MacroAwareGateway（事中 regime 否决）
    三层互补，构成事前→事中的完整风控链：
      - 事前（本模块）：宏观 regime 否决/HV 过滤/流动性过滤/仓位 5% 钳
      - 事中（risk_shield）：废单/超限/熔断拦截
      - 事中（MacroAwareGateway）：regime 变化时的实时否决

蔡森 Task 1 校准：
    停损 = C 波低点（在 Task 9 plan.py 实现，本模块 position_size 仅接收 entry/stop 参数，
    不关心停损来源，保持职责单一）。
"""
from __future__ import annotations
import math
import pandas as pd

from caisen.config import StrategyConfig


class RiskManager:
    """事前风控管理器（无状态计算，线程安全）。

    构造时延迟绑定 CreditRegime 单例（测试可注入），无宏观数据时降级保守半仓。
    """

    def __init__(self, cfg: StrategyConfig):
        """初始化事前风控。

        参数：
            cfg: 蔡森策略全参数模型（max_position_pct/hv_window 等风控阈值的真相源）。

        Why 延迟绑定 CreditRegime + try/except：
            macro 湖在离线/CI 环境不可用，import 期即硬绑会在测试链路触发湖读取。
            用 try/except + None 兜底，使本模块在任何环境都能 import；
            compute 路径会检查 regime is None 并安全降级（返 0.6 保守半仓）。
        """
        self.cfg = cfg
        # 延迟绑定 CreditRegime 单例（core.macro_regime.Phase 1 已从 factors 迁到 core）
        # 测试可通过直接赋值 rm.regime = FakeRegime() 注入替身。
        try:
            from core.macro_regime import CreditRegime
            self.regime = CreditRegime.get_default()
        except Exception:
            # 兜底：任何绑定异常（import 失败/湖未载入）都降级 None，
            # 由 macro_position_coef 的 None 分支安全保守返 0.6，绝不抛到筛形态主流程。
            self.regime = None

    def macro_position_coef(self, date) -> float:
        """宏观仓位系数 0~1（regime: +1→1.0, 0→0.6, -1→0.0）。

        物理意图：
            - +1 扩张：信贷宽松 + M1M2 活化 + DR007 下行 → 全额仓位（1.0）；
            - 0  中性：信号矛盾 → 保守 0.6（避免在非趋势行情过度暴露）；
            - -1 收缩：信贷收紧 + 资金沉淀 → macro_regime_veto=True 时一票否决（0.0），
              否则保留微量试探仓位（0.3）让短线形态仍有开仓可能。

        Args:
            date: 交易日（CreditRegime.compute 内部 .loc[:date] 严格无前视）。

        Returns:
            仓位系数 ∈ {0.0, 0.3, 0.6, 1.0}。
        """
        if self.regime is None:
            return 0.6   # 无宏观数据，保守半仓（避免无信息下重仓）
        r = self.regime.compute(date)
        if r == 1:
            return 1.0
        if r == -1:
            return 0.0 if self.cfg.macro_regime_veto else 0.3
        return 0.6

    def micro_filter(self, price_df: pd.DataFrame, symbol: str) -> tuple[bool, str]:
        """微观波动率过滤：近 hv_window 的 HV 分位 > hv_max_quantile → 剔除。

        物理意图（蔡森方法学）：
            蔡森形态学要求"有序震荡"——HV 处于历史高位的品种通常处于无序暴动
            （如政策冲击/地缘事件），颈线突破的统计有效性大幅下降。本过滤把这类
            标的在筛形态阶段直接剔除，避免给事中风控添堵。

        算法（显式 pandas，无黑盒）：
            1. 收益率 = close.pct_change()（dropna 去首日 NaN）；
            2. 滚动年化 HV = ret.rolling(hv_window).std() * sqrt(252)；
            3. 取近 hv_window 个 HV 样本，若末值 > 该窗口 hv_max_quantile（默认 95 分位）则否决。

        防御性兜底：
            - 样本不足（< hv_window）：放行（小样本分位无统计意义，不应误杀新股）；
            - HV 全 NaN：放行（数据异常不应成为否决理由，让后续流动性/形态过滤兜底）。

        Args:
            price_df: 标的行情 DataFrame（至少含 close 列）。
            symbol: 标的代码（仅用于剔除原因日志）。

        Returns:
            (True, "通过/放行原因") 或 (False, "剔除原因")。
        """
        ret = price_df["close"].pct_change().dropna()
        if len(ret) < self.cfg.hv_window:
            return True, "样本不足放行"
        hv = ret.rolling(self.cfg.hv_window).std() * math.sqrt(252)
        recent = hv.dropna().iloc[-self.cfg.hv_window:]
        if len(recent) == 0:
            return True, "HV 空放行"
        if recent.iloc[-1] > recent.quantile(self.cfg.hv_max_quantile):
            return False, f"{symbol} HV 异常(无序震荡)"
        return True, "通过"

    def liquidity_filter(self, price_df: pd.DataFrame) -> bool:
        """近 30 日平均成交额 ≥ liquidity_min_amount（默认 1 亿）。

        物理意图：
            形态交易依赖颈线突破后的流动性承接——成交额不足的标的（小盘/冷门）
            突破后易陷入"无量拉升→闪崩"陷阱，滑点失控且难以及时离场。
            1 亿门槛覆盖 A 股主流可交易标的，过滤边缘冷门壳股。

        防御性：
            - amount 列不 ffill（停牌日的 NaN 必须排除，不能以最近成交额填充伪装流动性）；
            - 30 日全 NaN（长期停牌）→ 直接否决（返 False）。

        Args:
            price_df: 标的行情 DataFrame（至少含 amount 列）。

        Returns:
            True 通过 / False 剔除。
        """
        amt = price_df["amount"].tail(30).dropna()
        if len(amt) == 0:
            return False
        # 强转 bool：pandas 标量比较返 numpy.bool_，调用方 `is True` 身份判定会误判，
        # 故显式转 Python 原生 bool 稳定契约（杜绝 np.True_ vs True 的歧义）。
        return bool(amt.mean() >= self.cfg.liquidity_min_amount)

    def position_size(self, aum: float, entry: float, stop: float, coef: float) -> int:
        """固定风险分配 + max_position_pct 硬钳 + A 股整手向下取整。

        物理意图（蔡森仓位管理 + A 股交易规则）：
            - 固定风险分配：每笔单的最大亏损 = 仓位 × (entry - stop)，
              按"理论上愿意为这笔单承受的最大亏损"分配股数；
            - 但同时受 max_position_pct（5%）双重硬钳：单标的市值不得超过总资金 5%
              （集中度风控，防单票黑天鹅击穿组合）；
            - A 股最小交易单位 100 股（整手），向下取整避免拆零股废单。

        算法（显式数学，无黑盒）：
            risk_per_share = max(entry - stop, 1e-9)   # 防除零：停损贴近 entry 时下限保护
            max_capital    = aum × max_position_pct × coef   # 5% 上限 × 宏观系数
            shares         = max_capital / risk_per_share    # 按风险反推股数
            shares         = min(shares, (aum × max_position_pct) / entry)  # 再被 5% 市值钳
            return         = floor(shares / 100) × 100       # 向下取整到 100 整手

        Args:
            aum:   账户总资金（Asset Under Management）。
            entry: 入场价（颈线满足点，由 Task 9 plan.py 计算）。
            stop:  停损价（蔡森 Task 1 校准 = C 波低点，由 Task 9 plan.py 计算）。
            coef:  宏观仓位系数（macro_position_coef 的返回值）。

        Returns:
            整手股数（int，且为 100 的整数倍，下限 0）。
        """
        risk_per_share = max(entry - stop, 1e-9)
        max_capital = aum * self.cfg.max_position_pct * coef   # 5% 上限 × 宏观系数
        shares = max_capital / risk_per_share
        shares = min(shares, (aum * self.cfg.max_position_pct) / entry)  # 再被 5% 市值钳
        return max(0, int(shares // 100) * 100)
