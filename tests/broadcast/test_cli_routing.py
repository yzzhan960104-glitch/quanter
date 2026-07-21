# -*- coding: utf-8 -*-
"""broadcast CLI --bot 路由 + 幂等单测（Task 2）。

只覆盖框架性能力（Task 2 仅搭骨架，Task 3-5 才接 trading/data/strategy 真实 brief）：
  1. 每个机器人有独立幂等文件，互不干扰（防跨机器人误判重复）
  2. 未知 bot 抛 ValueError（防 CLI 笔误静默落到 market）
"""
from broadcast import __main__ as bc


def test_last_brief_path_per_bot():
    """每个机器人独立幂等文件，互不干扰。"""
    assert bc.last_brief_file("market").name == ".last_market_brief"
    assert bc.last_brief_file("trading").name == ".last_trading_brief"
    assert bc.last_brief_file("data").name == ".last_data_brief"
    assert bc.last_brief_file("strategy").name == ".last_strategy_brief"


def test_last_brief_file_unknown_bot(tmp_path, monkeypatch):
    """未知 bot 抛 ValueError（防误用）。"""
    try:
        bc.last_brief_file("unknown")
        assert False, "应抛 ValueError"
    except ValueError:
        pass
