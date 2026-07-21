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
