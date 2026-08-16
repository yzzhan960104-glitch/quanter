# -*- coding: utf-8 -*-
"""CLI 端到端：create/promote/set-weight/archive/rollback/list。用 monkeypatch 切 db 路径。"""
import json

import pytest

from experiment import cli, store, resolver
from experiment.models import ExperimentStatus


@pytest.fixture
def db(tmp_path, monkeypatch):
    """CLI 默认走 experiment/experiments.db，测试 monkeypatch 到临时路径。

    注：v1 plan 只 patch cli._DEFAULT_DB，但 test_cli_set_weight_archive_rollback 里
    resolver.resolve_active() 未传 db_path → 读 resolver 模块引用的 _DEFAULT_DB（默认真实路径）。
    此处额外 patch resolver._DEFAULT_DB 保证一致性（不改实现/接口，仅修测试 fixture）。
    """
    p = str(tmp_path / "t.db")
    store.init_db(p)
    monkeypatch.setattr(cli, "_DEFAULT_DB", p)
    monkeypatch.setattr(resolver, "_DEFAULT_DB", p)
    return p


def test_cli_create_promote_list(db, capsys):
    """create → promote → list 全链路。"""
    rc = cli.main(["create", "--strategy", "neckline",
                   "--params", '{"window": 60}', "--experiment-id", "e1",
                   "--source", "manual", "--created-at", "2026-07-22T10:00:00"])
    assert rc == 0
    rc = cli.main(["promote", "e1", "--weight", "0.5"])
    assert rc == 0
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "e1" in out and "ACTIVE" in out


def test_cli_set_weight_archive_rollback(db):
    """set-weight → archive → rollback。"""
    cli.main(["create", "--strategy", "neckline", "--params", '{}',
              "--experiment-id", "e1", "--created-at", "t"])
    cli.main(["promote", "e1", "--weight", "0.3"])
    cli.main(["set-weight", "e1", "--weight", "0.6"])
    assert resolver.resolve_active()[0].weight == 0.6
    cli.main(["archive", "e1"])
    assert resolver.resolve_active() == []
    cli.main(["rollback", "e1"])
    assert resolver.resolve_active()[0].experiment_id == "e1"


def test_cli_promote_rejects_overflow(db, capsys):
    """CLI 层权重溢出报错（非零退出）。"""
    cli.main(["create", "--strategy", "neckline", "--params", '{}',
              "--experiment-id", "e1", "--created-at", "t"])
    cli.main(["promote", "e1", "--weight", "0.8"])
    cli.main(["create", "--strategy", "neckline", "--params", '{}',
              "--experiment-id", "e2", "--version", "2", "--created-at", "t"])
    rc = cli.main(["promote", "e2", "--weight", "0.3"])  # 0.8+0.3=1.1
    assert rc != 0
    err = capsys.readouterr().err
    assert "权重" in err


def test_cli_discard_draft_writes_audit_note(db):
    """T2.2+review 修复：discard --note 落审计行（原死参数，审计只记 status）。"""
    cli.main(["create", "--strategy", "neckline", "--params", '{}',
              "--experiment-id", "e1", "--created-at", "t"])
    rc = cli.main(["discard", "e1", "--note", "autopromote G2 未过"])
    assert rc == 0
    assert store.list_versions(db, status=ExperimentStatus.DRAFT) == []
    assert store.list_versions(db, status=ExperimentStatus.ARCHIVED)[0].experiment_id == "e1"
    dc = [a for a in store.list_audit(db, "e1") if a.action == "discard"][0]
    assert dc.changed_fields.get("reason") == "autopromote G2 未过"


def test_cli_autopromote_latest_without_positional_id(db, monkeypatch, capsys):
    """review 修复回归（T3.4 cron 必崩）：autopromote --latest 不带位置参数可解析——
    原实现 experiment_id 必填，cron 只传 --latest 会 argparse exit 2，日报从未跑通。"""
    cli.main(["create", "--strategy", "neckline", "--params", '{}',
              "--experiment-id", "d1", "--created-at", "t"])   # 造一条 DRAFT 供 --latest 选中
    from research import autopromote as _ap
    monkeypatch.setattr(_ap, "run",
                        lambda target, **kw: {"action": "stub", "target": target})
    rc = cli.main(["autopromote", "--latest"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stub" in out


def test_cli_autopromote_requires_id_or_latest(db, capsys):
    """无 id 无 --latest → 显式报错非零退出（fail-fast 给人看清用法）。"""
    rc = cli.main(["autopromote"])
    assert rc == 1
    assert "experiment_id" in capsys.readouterr().err


def test_cli_discard_rejects_active(db, capsys):
    """T2.2：CLI discard 对 ACTIVE 报错非零退出（防借 discard 绕 archive 语义）。"""
    cli.main(["create", "--strategy", "neckline", "--params", '{}',
              "--experiment-id", "e1", "--created-at", "t"])
    cli.main(["promote", "e1", "--weight", "0.5"])
    rc = cli.main(["discard", "e1"])
    assert rc != 0
    assert "仅 DRAFT" in capsys.readouterr().err
