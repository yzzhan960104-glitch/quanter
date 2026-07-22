# -*- coding: utf-8 -*-
"""trading.compute.breaker — 日内权益回撤熔断判定纯函数（functional core）。

物理定位（Layer2 阶段2 · spec §3.5/§4）：
    check_daily_loss_limit 是【纯判定】——输入两条权益 + 阈值，输出布尔。仅依赖
    标准库（os 读取 env 作为 fallback，属配置读取范畴，purity 测试白名单允许）。
    回测与实盘共用同一熔断判定（杀手不变量）。

物理意图（Why 风控阈值定 -3%）：
    日内 3% 权益回撤在 A 股单一策略层已属显著异常（多数交易日波动远小于此），
    一旦触及即视作「策略与环境失配」的强信号，宁可当日停手、次日复盘重启，
    也不容忍异常持续累积成穿仓。

迁移纪律（strangler 红线①）：判定逻辑【零改动】，只搬位置（trading/circuit_breaker.py
→ trading/compute/breaker.py）。副作用函数 cancel_all_open_orders（撤未终态单）留
原处（trading/circuit_breaker.py · I/O 域），不进 compute。原模块经垫片 re-export。
"""
from __future__ import annotations

import os


def check_daily_loss_limit(
    start_equity: float,
    curr_equity: float,
    *,
    limit: float | None = None,
) -> bool:
    """判定日内权益回撤是否触及熔断上限。

    参数：
        start_equity: 当日开盘基线权益（如前一日收盘总资产）。
        curr_equity:  当前实时权益（盘中最新总资产）。
        limit:        负数熔断阈值，如 ``-0.03`` 表示亏 3% 即熔断；
                      None 则读 env ``CIRCUIT_DAILY_LOSS_LIMIT``，缺省 -0.03。

    返回：
        True 表示已触及/穿透熔断线，应进入熔断流程（lock_down + 撤单 + 告警）。

    边界：
    - ``start_equity <= 0`` 直接返回 False——既防除零，也表达「无有效基线权益
      时不应贸然触发熔断」（如冷启动首日未拿到准确基线，让引擎继续运行由
      其他维度兜底，避免除零异常使整条熔断链路失效）。
    - 采用 ``<=`` 而非 ``<``：恰触阈值即触发，风控宁可早一拍停手也不容忍
      边界继续裸奔（与 order_state.check_stop_loss 的判定口径对称）。
    """
    if limit is None:
        # env 缺省 -0.03：未显式配置时采用保守默认，避免线上裸奔。
        limit = float(os.getenv("CIRCUIT_DAILY_LOSS_LIMIT", "-0.03"))
    if start_equity <= 0:
        return False
    pnl_pct = (curr_equity - start_equity) / start_equity
    return pnl_pct <= limit
