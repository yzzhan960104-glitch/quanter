# -*- coding: utf-8 -*-
"""统一同步 CLI 参数解析 + key 过滤 + fail-soft 测试（Plan Task 9）。

物理意图：把散装 scripts/sync_*.py 能力收敛到 python -m data.sync，底层复用 sync_dataset。
"""
from unittest.mock import patch
import pytest

from data.sync_cli import parse_args, select_keys, run


def test_parse_args_全量():
    args = parse_args(["--all", "--since", "2021-01-01"])
    assert args.all is True
    assert args.since == "2021-01-01"
    assert args.quota is None


def test_parse_args_指定keys():
    args = parse_args(["--keys", "daily,weekly", "--since", "2021-01-01"])
    assert args.keys == ["daily", "weekly"]  # 逗号分隔解析为列表
    assert args.all is False


def test_parse_args_dry_run():
    args = parse_args(["--keys", "moneyflow", "--dry-run"])
    assert args.dry_run is True


def test_parse_args_quota过滤():
    args = parse_args(["--all", "--quota", "special"])
    assert args.quota == "special"


def test_select_keys_按quota过滤():
    """--quota basic 只选基础桶 key（排除 special 的 moneyflow）。"""
    basic = select_keys(all_keys=True, keys=None, quota="basic")
    assert "stock_basic" in basic
    assert "moneyflow" not in basic  # moneyflow 是 special


def test_select_keys_指定keys():
    sel = select_keys(all_keys=False, keys=["daily", "weekly"], quota=None)
    assert sel == ["daily", "weekly"]


def test_run_单key失败不中断后续(monkeypatch):
    """fail-soft：某 key 抛异常，后续 key 仍跑，汇总 exit code=1。"""
    calls = []
    def fake_sync(key, start, end, **kw):
        calls.append(key)
        if key == "bad":
            raise RuntimeError("故意失败")
    monkeypatch.setattr("data.sync_cli.sync_dataset", fake_sync)
    rc = run(keys=["good1", "bad", "good2"], since="2021-01-01", end="2021-01-02")
    assert rc == 1  # 部分失败
    assert calls == ["good1", "bad", "good2"]  # bad 之后 good2 仍执行


def test_run_全成功返0(monkeypatch):
    monkeypatch.setattr("data.sync_cli.sync_dataset", lambda key, start, end, **kw: None)
    rc = run(keys=["a", "b"], since="2021-01-01", end="2021-01-02")
    assert rc == 0
