# -*- coding: utf-8 -*-
"""动态白名单（信号标的临时注入 risk_shield 白名单，当日有效）。

物理意图（Why）：
.env QMT_SYMBOL_WHITELIST 是静态兜底（生产为 4 只 ETF 池子）；二期自动交易引擎
每日扫出的颈线法标的含创板/科创个股，不在静态白名单内，会被 risk_shield.check_order
关5（symbol not in whitelist → blocked）一刀切拒单。引擎必须在 pre_open 把当日
计划标的临时注入白名单（过挡板），盘后 post_close 清掉，不污染静态配置。

跨进程语义（Why 模块级 _DYNAMIC 不跨进程同步）：
engine 是独立常驻进程（python -m trading），server（uvicorn）是另一个进程。
模块级 _DYNAMIC 只在 engine 进程内有效 →
- engine 自动下单：受动态白名单影响（正确，计划标的需要过关5）；
- server 手动下单（前端/Cockpit）：_DYNAMIC 恒空 = 纯 env，行为与改造前完全一致
  （向后兼容，前端手动下单语义不被 engine 内部状态污染）。
这是设计预期，不要试图用 Redis/文件锁跨进程共享。
"""
from __future__ import annotations

import os

# 模块级全局：动态注入集合。模块级而非类/实例——引擎是单进程单调度器，
# 无需对象化封装；进程退出即失效（当日有效的物理语义天然满足）。
_DYNAMIC: set[str] = set()


def inject_dynamic_whitelist(symbols: set[str]) -> None:
    """注入当日计划标的（合并到动态集合，不去重——set.update 天然幂等）。

    语义选择 update 而非替换：允许 engine 在 pre_open 注入主计划后，
    盘中 stop_loss_monitor（若有补仓信号）增量追加，不必重放全集。
    """
    _DYNAMIC.update(symbols)


def clear_dynamic_whitelist() -> None:
    """清空动态白名单（post_close 调用，保证下一交易日从干净状态开始）。

    Why 必须显式清而非依赖进程重启：引擎是常驻进程跨交易日，不清空则
    昨日标的会污染今日白名单，可能导致今日未计划标的过关5（前视污染）。
    """
    _DYNAMIC.clear()


def get_effective_whitelist() -> set[str]:
    """有效白名单 = 静态 env（逗号分隔）∪ 动态注入。

    返回新 set（`|` 运算符天然返回新对象），调用方修改不影响内部 _DYNAMIC。
    _DYNAMIC 为空时 = 纯 env 解析结果，与旧 trading_service._whitelist() 等价
    （向后兼容红线：server 进程 _DYNAMIC 恒空，行为不变）。
    """
    # 静态解析：逗号分隔 + strip + 丢空串（与 trading_service._whitelist 旧实现
    # 逐字一致，保证 server 进程的等价性）
    static = {s.strip() for s in os.getenv("QMT_SYMBOL_WHITELIST", "").split(",") if s.strip()}
    return static | _DYNAMIC
