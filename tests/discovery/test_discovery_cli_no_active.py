# -*- coding: utf-8 -*-
"""B3 M-1 fast 单测：cli cmd_oos / cmd_verify 无 ACTIVE → SystemExit(2) 早返路径。

物理意图（2026-08-05 双轨治理收口 → SSoT review-fix2 P2 统一选择口径）：
    ``discovery.cli.cmd_oos`` / ``cmd_verify`` 是人审探查工具，冠军参数从
    ``experiment.db ACTIVE``（resolve_champion 单一选择口径，实盘 _eod 同源）读。
    设计语义：**无 ACTIVE 实验 → 拒绝运行（exit 2）**——没有当前生效冠军，
    oos/verify 无从跑「当前冠军」去偏，应立即可见地失败而非静默走默认/legacy。

    本 fast 单测覆盖该早返路径：monkeypatch ``resolve_champion`` 返 ``None``（无 ACTIVE），
    调 ``cmd_oos``/``cmd_verify``，断言 ``SystemExit(2)`` + stderr 含提示语。
    **不跑真 oos/verify**（真跑需 freeze 全 data_lake + 全历史 scan_symbol ~3-4min，
    在 ``test_cli_oos.py::test_oos_command_produces_report`` 的 slow 标记下）。

    早返发生在 args 字段访问之前（``champion = resolve_champion()`` → ``if champion is None: sys.exit(2)``
    先于 ``args.embargo``/``args.perturb``），故 ``args`` 用最小 stub 即可。
"""
import pytest


def test_cmd_oos_no_active_exits_2(monkeypatch):
    """无 ACTIVE 实验 → cmd_oos sys.exit(2)，不跑真 oos（fast 早返路径覆盖）。"""
    from types import SimpleNamespace
    from discovery import cli

    # monkeypatch resolve_champion 返 None（无 ACTIVE）
    monkeypatch.setattr(cli, "resolve_champion", lambda: None)

    args = SimpleNamespace(embargo=5)  # 字段不触达（早返在 args.embargo 之前）
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_oos(args)
    assert excinfo.value.code == 2, (
        f"无 ACTIVE cmd_oos 应 exit 2（拒绝运行），got exit code={excinfo.value.code}"
    )


def test_cmd_verify_no_active_exits_2(monkeypatch):
    """无 ACTIVE 实验 → cmd_verify sys.exit(2)，不跑真 verify（fast 早返路径覆盖）。"""
    from types import SimpleNamespace
    from discovery import cli

    monkeypatch.setattr(cli, "resolve_champion", lambda: None)

    args = SimpleNamespace(perturb=1, n_samples=8)  # 字段不触达（早返在前）
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_verify(args)
    assert excinfo.value.code == 2, (
        f"无 ACTIVE cmd_verify 应 exit 2（拒绝运行），got exit code={excinfo.value.code}"
    )


def test_cmd_oos_no_active_stderr_message(monkeypatch, capsys):
    """无 ACTIVE → stderr 含 'promote 一个实验' 提示（人审可操作的失败语义）。"""
    from types import SimpleNamespace
    from discovery import cli

    monkeypatch.setattr(cli, "resolve_champion", lambda: None)

    args = SimpleNamespace(embargo=5)
    with pytest.raises(SystemExit):
        cli.cmd_oos(args)
    err = capsys.readouterr().err
    assert "promote 一个实验" in err, (
        f"无 ACTIVE stderr 应含 promote 提示（人审可操作），got stderr={err!r}"
    )
