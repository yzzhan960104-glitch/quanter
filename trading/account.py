# -*- coding: utf-8 -*-
"""账户 ID 解析单一真相源（H3/T2 收口 · 2026-08-12）。

Why 独立模块：engine / eod_plan / veto_plan / trading_service 原各自复制 _resolve_account_id
（2 行 + 注释锁，避免循环 import engine）。口径必须一致——否则 eod_plan 写 account_A，
pre_open/veto 读 account_B，trade_id 对不上 → SIGNAL/CONFIRMED/VETOED 防线全部失效（致命）。
本模块为单一真相源，四处改 import 此处。

phases 经 `_eng_mod._resolve_account_id` 反查 engine 的 wrapper（保留为测试 patch 目标），
W1-A 切断 _eng_mod 时再改指本模块。
"""
from __future__ import annotations

import os


def resolve_account_id() -> str:
    """当前账户 ID：QMT_ACCOUNT_ID env 优先，缺失走 state_store 默认账户。

    物理意图：trade_event/order 等 UNIQUE 键含 account_id，跨模块必须同口径。
    启动期 _migrate_env_to_account 已把 env 账户落库；dry_run 无 broker 配置时
    用 state_store._DEFAULT_ACCOUNT_ID（保证测试/影子期也有稳定 account_id）。
    """
    aid = os.getenv("QMT_ACCOUNT_ID")
    if aid:
        return aid
    # lazy import：account 不顶层依赖 state_store（state_store 不 import account，无环）
    from trading import state_store
    return state_store._DEFAULT_ACCOUNT_ID
