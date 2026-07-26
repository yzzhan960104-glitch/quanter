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


def test_no_subcommand_routes_to_push(monkeypatch):
    """C1 红线：无子命令 = push（schtasks 生成的 'python -m broadcast --bot trading' 零改动）。"""
    routed = {}
    monkeypatch.setattr(bc, "_main_push", lambda a: routed.setdefault("push", a) or 0)
    bc.main(["--bot", "trading", "--dry-run"])
    assert routed.get("push") == ["--bot", "trading", "--dry-run"]


def test_explicit_push_subcommand_routes(monkeypatch):
    routed = {}
    monkeypatch.setattr(bc, "_main_push", lambda a: routed.setdefault("push", a) or 0)
    bc.main(["push", "--bot", "data"])
    assert routed.get("push") == ["--bot", "data"]


def test_connect_subcommand_routes(monkeypatch):
    """connect 首参 → _main_connect，不再走 push。"""
    routed = {}
    monkeypatch.setattr(bc, "_main_connect", lambda a: routed.setdefault("connect", a) or 0)
    bc.main(["connect", "--status"])
    assert routed.get("connect") == ["--status"]


def test_connect_start_all_prompts_confirm(monkeypatch, capsys):
    """--start all 二次确认：输入非 y → 取消，不拉起任何 bot（防误启 5 个 Claude Code）。"""
    monkeypatch.setattr(bc, "_read_confirm", lambda: "n")
    monkeypatch.setattr(bc.connect_manager, "start", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应拉起")))
    rc = bc.main(["connect", "--start", "all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "取消" in out


def test_connect_status_calls_manager(monkeypatch, capsys):
    """--status 遍历 CONNECT_BOTS 调 connect_manager.status。"""
    reported = {}
    monkeypatch.setattr(bc.connect_manager, "status", lambda bot: reported.setdefault(bot, "running"))
    rc = bc.main(["connect", "--status"])
    assert rc == 0
    assert set(reported.keys()) == set(bc.CONNECT_BOTS)  # 5 个全报
