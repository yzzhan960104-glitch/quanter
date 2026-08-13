# -*- coding: utf-8 -*-
"""trading.compute.breaker — 日内权益回撤熔断判定（DG-G3 fail-closed 收口）。

物理定位（Layer2 阶段2 · spec §3.5/§4 + DG-G3 · 2026-08-13）：
    check_daily_loss_limit 原是【纯判定 functional core】——输入两条权益 + 阈值，输出布尔。
    DG-G3 后**基线缺失分支升为 fail-closed 副用**（告警 + live 模式 raise _CriticalHalt），
    有基线路径仍保持纯判定（行为等价红线，spec G3「有基线路径行为不变」）。

    Why 在判定层加副用（设计权衡 · DG-G3 裁决）：
        原纯判定 + ``return False`` fail-open 语义，在「account_daily 漏采 + T-1 close 兜底
        也取不到」的极端情形会静默放行（日内 -3% 熔断失效 → 实盘敞口失控红线）。
        post_close 的 T-1 兜底是【业务层】第一道防线（写到 account_daily.start），
        但若兜底也失败，判定层本身必须 fail-closed（不能依赖调用方正确处理 None）。
        副用收口在判定层 = 「基线链全失效时绝不放行」的最后一道闸，spec §3.5 纯判定
        契约在此处让位于风控语义（DG-G3 决议明示）。

物理意图（Why 风控阈值定 -3%）：
    日内 3% 权益回撤在 A 股单一策略层已属显著异常（多数交易日波动远小于此），
    一旦触及即视作「策略与环境失配」的强信号，宁可当日停手、次日复盘重启，
    也不容忍异常持续累积成穿仓。

迁移纪律（strangler 红线①）：判定逻辑【零改动】，只搬位置（trading/circuit_breaker.py
→ trading/compute/breaker.py）。副作用函数 cancel_all_open_orders（撤未终态单）留
原处（trading/circuit_breaker.py · I/O 域），不进 compute。原模块经垫片 re-export。
DG-G3 在判定层引入的副用（_alert_critical / _mode / raise _CriticalHalt）仅限基线缺失
分支，有基线路径零副用（purity 行为等价）。
"""
from __future__ import annotations

import os

# DG-G3 fail-closed 副用收口（仅基线缺失分支触达，有基线路径不调）：
# - _alert_critical：致命事件钉钉 CRITICAL（fire_and_forget，与 pre_open/post_close 同通道）；
# - _CriticalHalt：L1 致命异常（live 模式 raise → engine._critical_guard 捕获 _halt 停调度）；
# - _mode：交易模式 env 读口（dry_run 默认 / live 显式）。
# 三者均来自 trading.critical（集群 A 基础设施叶子模块，无循环依赖）。
from trading.critical import _CriticalHalt, _alert_critical, _mode


def check_daily_loss_limit(
    start_equity: float | None,
    curr_equity: float,
    *,
    limit: float | None = None,
) -> bool:
    """判定日内权益回撤是否触及熔断上限（基线缺失 fail-closed · DG-G3）。

    参数：
        start_equity: 当日开盘基线权益（如前一日收盘总资产）。**None 表示基线链全失效**
                      （account_daily.start 漏采 + post_close T-1 兜底也取不到）。
        curr_equity:  当前实时权益（盘中最新总资产）。
        limit:        负数熔断阈值，如 ``-0.03`` 表示亏 3% 即熔断；
                      None 则读 env ``CIRCUIT_DAILY_LOSS_LIMIT``，缺省 -0.03。

    返回：
        True 表示已触及/穿透熔断线，应进入熔断流程（lock_down + 撤单 + 告警）。
        **基线缺失时也返 True**（C-1 当日停手语义，DG-G3 fail-closed）。

    边界（DG-G3 fail-closed · 2026-08-13）：
    - ``start_equity is None or start_equity <= 0``：基线链全失效 → **不再 return False 放行**
      （原 fail-open 让日内 -3% 熔断静默失效 = 实盘敞口失控红线）。改为触发保护：
        - dry_run（模拟盘）：返 True（C-1 当日停手）+ CRITICAL 告警，**不抛 halt 进程**
          （影子观测/回放态不应中断引擎，与 live 区分）；
        - live（实盘）：raise _CriticalHalt（engine._critical_guard 捕获 → _halt L1 停调度，
          与 pre_open DB 写失败同 _CriticalHalt 语义）。
      DG-G3 裁决：不选「仅告警不动作」（那是 fail-open 残留，本 G3 收尾）。
    - 采用 ``<=`` 而非 ``<``：恰触阈值即触发，风控宁可早一拍停手也不容忍
      边界继续裸奔（与 order_state.check_stop_loss 的判定口径对称）。
    - 有基线路径（start_equity>0）判定逻辑【零变更】（行为等价红线，spec G3）。
    """
    if limit is None:
        # env 缺省 -0.03：未显式配置时采用保守默认，避免线上裸奔。
        limit = float(os.getenv("CIRCUIT_DAILY_LOSS_LIMIT", "-0.03"))
    if start_equity is None or start_equity <= 0:
        # DG-G3 fail-closed（2026-08-13）：基线链全失效（account_daily.start 漏采 +
        # post_close T-1 close 兜底也取不到）→ 绝不放行。
        # Why 副用收口在判定层：原 ``return False`` fail-open 让熔断静默失效（实盘敞口
        # 失控红线），post_close 的 T-1 兜底是业务层第一道防线，本层是「基线全失」的
        # 最后一道闸——不能依赖调用方正确处理 None（防御性深度）。
        _alert_critical(
            "熔断基线缺失（account_daily.start NULL 且 T-1 close 兜底也无效），"
            "fail-closed 触发保护")
        if _mode() == "live":
            # live 模式 = 真金敞口失控红线，停调度等人工介入（与 pre_open DB 写失败
            # 同 _CriticalHalt 语义，engine._critical_guard 捕获后 _halt）。
            raise _CriticalHalt(
                "熔断基线缺失（account_daily.start NULL），live 拒绝继续下注")
        # dry_run 模式：返 True（C-1 当日停手语义），不抛 halt 进程——影子观测/回放态
        # 不应中断引擎，与 live 模式的停调度语义区分（DG-G3 裁决）。
        return True
    pnl_pct = (curr_equity - start_equity) / start_equity
    return pnl_pct <= limit
