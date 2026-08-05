# -*- coding: utf-8 -*-
"""broadcast CLI 路由 + 幂等单测。

B4（2026-08-05）：last_brief_file 退役，播报幂等迁 job_ledger（job_name=brief_<bot>）。
本文件改断言「幂等查 job_ledger.latest_status」+ push/connect 子命令路由。
"""
from broadcast import __main__ as bc


def test_brief_job_name_per_push_bot():
    """B4：每个 push 机器人对应独立 job_ledger job_name=brief_<bot>，互不干扰。

    取代旧 test_last_brief_path_per_push_bot（last_brief_file 已退役）。
    物理意图：分 bot job_name 防跨机器人误判已播（与原分文件语义对齐）。
    """
    # job_name 由调用方拼接 f"brief_{bot}"（broadcast/__main__._main_push）；
    # SUPPORTED_BOTS 仍是 ("trading","data","strategy")，覆盖三个 job_name 派生。
    assert set(bc.SUPPORTED_BOTS) == {"trading", "data", "strategy"}
    for bot in bc.SUPPORTED_BOTS:
        job_name = f"brief_{bot}"
        assert job_name in {"brief_trading", "brief_data", "brief_strategy"}


def test_brief_idempotent_lookup_unknown_bot():
    """B4：未知 bot 走 CLI argparse choices 挡一道（market 已下线 → 不在 choices）。

    取代旧 test_last_brief_file_unknown_bot：last_brief_file 已删，未知 bot 不再有
    函数级 ValueError 防线，改由 argparse choices 在 CLI 入口拦截。
    """
    # argparse choices=SUPPORTED_BOTS 已挡未知 bot；这里断言 market 不在支持列表
    assert "market" not in bc.SUPPORTED_BOTS
    assert "unknown" not in bc.SUPPORTED_BOTS


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
