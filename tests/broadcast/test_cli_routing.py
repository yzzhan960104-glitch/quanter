# -*- coding: utf-8 -*-
"""broadcast CLI 路由 + 幂等单测。

Task 3：market 下线后，last_brief_file 仅服务 push 类（trading/data/strategy），
未知 bot 抛 ValueError（防 CLI 笔误静默落到默认 bot）。
Task 4 将在此补 push/connect 子命令路由断言。
"""
from broadcast import __main__ as bc


def test_last_brief_path_per_push_bot():
    """每个 push 机器人独立幂等文件，互不干扰（防跨机器人误判重复）。"""
    assert bc.last_brief_file("trading").name == ".last_trading_brief"
    assert bc.last_brief_file("data").name == ".last_data_brief"
    assert bc.last_brief_file("strategy").name == ".last_strategy_brief"


def test_last_brief_file_unknown_bot():
    """未知 bot 抛 ValueError（防误用）。"""
    try:
        bc.last_brief_file("market")  # market 已下线 → 视为未知
        assert False, "应抛 ValueError"
    except ValueError:
        pass
    try:
        bc.last_brief_file("unknown")
        assert False, "应抛 ValueError"
    except ValueError:
        pass


def test_supported_bots_no_market():
    """SUPPORTED_BOTS 不含 market（下线红线）。"""
    assert "market" not in bc.SUPPORTED_BOTS
    assert set(bc.SUPPORTED_BOTS) == {"trading", "data", "strategy"}
